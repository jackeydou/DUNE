"""Calling a backend: request adaptation, the call itself, and normalizing what comes back.

The only module that imports the `openai` SDK (AGENTS.md "Model calls").
"""

import contextvars
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import httpx2
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, omit
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam, ChatCompletionToolParam
from pydantic import JsonValue

from swarmeval.gateway.model.config import Backend, GatewayConfig, ModelRoute, ReasoningPassback
from swarmeval.gateway.model.wire import (
    ChatRequest,
    ChatResponse,
    WireAssistantMessage,
    WireChoice,
    WireFunctionCall,
    WireMessage,
    WireToolCall,
    WireUsage,
    WireUserMessage,
)


class UnknownModelError(Exception):
    pass


class UpstreamError(Exception):
    """The backend failed the call after the SDK's retries."""

    def __init__(self, message: str, status: int | None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class UpstreamResult:
    response: ChatResponse
    raw_json: str
    backend: str
    route: ModelRoute
    served_model: str
    passback: ReasoningPassback
    weights_hash: str | None
    sampling: dict[str, JsonValue]
    latency_s: float
    attempts: int


_attempts: contextvars.ContextVar[list[int]] = contextvars.ContextVar("attempts")


async def _count_attempt(request: httpx2.Request) -> None:
    counter = _attempts.get(None)
    if counter is not None:
        counter[0] += 1


def passback(messages: list[WireMessage], mode: ReasoningPassback) -> list[WireMessage]:
    """Past reasoning to send upstream. `within_turn` keeps it only after the last user message,
    which is the reasoning of the tool-calling turn still in progress."""
    if mode == "all":
        return messages
    last_user = max(
        (i for i, m in enumerate(messages) if isinstance(m, WireUserMessage)), default=-1
    )
    kept: list[WireMessage] = []
    for i, message in enumerate(messages):
        keep = mode == "within_turn" and i > last_user
        if isinstance(message, WireAssistantMessage) and not keep:
            message = message.model_copy(update={"reasoning_content": None})
        kept.append(message)
    return kept


TOOL_IMAGES_LABEL = "Images from tool call {call_id}:"


def move_tool_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool messages with text only, as Chat Completions requires, each run of them followed by a
    user message holding their images, each image labelled with its tool call. Runs after
    `passback`, so the added user messages do not move its turn boundary."""
    out: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    for message in messages:
        if message["role"] != "tool" and images:
            out.append({"role": "user", "content": images})
            images = []
        content = message.get("content")
        if message["role"] == "tool" and isinstance(content, list):
            parts = cast(list[dict[str, Any]], content)
            texts = [p["text"] for p in parts if p["type"] == "text"]
            pictures = [p for p in parts if p["type"] == "image_url"]
            message = {**message, "content": "\n".join(texts)}
            if pictures:
                label = TOOL_IMAGES_LABEL.format(call_id=message["tool_call_id"])
                images += [{"type": "text", "text": label}, *pictures]
        out.append(message)
    if images:
        out.append({"role": "user", "content": images})
    return out


class Upstreams:
    """One SDK client per backend, created on first use."""

    def __init__(
        self,
        config: GatewayConfig,
        transports: Mapping[str, httpx2.AsyncBaseTransport] | None = None,
    ) -> None:
        """`transports` replaces the network for a backend, by name. Tests only."""
        self._config = config
        self._transports = dict(transports or {})
        self._clients: dict[str, AsyncOpenAI] = {}

    def models(self) -> list[str]:
        return sorted(self._config.models)

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()

    def _client(self, name: str, backend: Backend) -> AsyncOpenAI:
        client = self._clients.get(name)
        if client is None:
            http = httpx2.AsyncClient(
                transport=self._transports.get(name),
                event_hooks={"request": [_count_attempt]},
            )
            client = AsyncOpenAI(
                base_url=backend.base_url,
                api_key=backend.api_key(),
                timeout=backend.timeout_s,
                max_retries=backend.max_retries,
                http_client=http,
            )
            self._clients[name] = client
        return client

    async def complete(self, request: ChatRequest) -> UpstreamResult:
        route = self._config.models.get(request.model)
        if route is None:
            raise UnknownModelError(
                f"model `{request.model}` is not served here. Models: {', '.join(self.models())}."
            )
        backend = self._config.backends[route.backend]
        defaults = backend.defaults
        temperature = _first(request.temperature, defaults.temperature)
        top_p = _first(request.top_p, defaults.top_p)
        max_tokens = _first(request.max_completion_tokens, defaults.max_completion_tokens)
        seed = _first(request.seed, defaults.seed)
        sampling: dict[str, JsonValue] = {
            k: v
            for k, v in {
                "temperature": temperature,
                "top_p": top_p,
                "max_completion_tokens": max_tokens,
                "seed": seed,
            }.items()
            if v is not None
        }
        messages = move_tool_images(
            [
                m.model_dump(exclude_none=True)
                for m in passback(request.messages, backend.reasoning_passback)
            ]
        )
        tools = [t.model_dump() for t in request.tools or ()]
        counter = [0]
        token = _attempts.set(counter)
        started = time.monotonic()
        try:
            client = self._client(route.backend, backend)
            raw = await client.chat.completions.with_raw_response.create(
                model=route.upstream_model,
                messages=cast(list[ChatCompletionMessageParam], messages),
                tools=cast(list[ChatCompletionToolParam], tools) if tools else omit,
                temperature=omit if temperature is None else temperature,
                top_p=omit if top_p is None else top_p,
                max_completion_tokens=omit if max_tokens is None else max_tokens,
                seed=omit if seed is None else seed,
            )
        except APIStatusError as err:
            raise UpstreamError(
                f"backend `{route.backend}` answered {err.status_code} for model "
                f"`{route.upstream_model}`: {err.message}",
                err.status_code,
            ) from err
        except APIConnectionError as err:
            raise UpstreamError(
                f"backend `{route.backend}` at {backend.base_url} is unreachable: {err}", None
            ) from err
        finally:
            _attempts.reset(token)
        latency = time.monotonic() - started
        completion = raw.parse()
        if not completion.choices or completion.usage is None:
            raise UpstreamError(
                f"backend `{route.backend}` returned a response for `{route.upstream_model}` "
                "without choices or token usage; budgets need usage. Body: " + raw.text[:500],
                None,
            )
        return UpstreamResult(
            response=normalize(completion, request.model),
            raw_json=raw.text,
            backend=route.backend,
            route=route,
            served_model=completion.model,
            passback=backend.reasoning_passback,
            weights_hash=backend.weights_hash,
            sampling=sampling,
            latency_s=latency,
            attempts=counter[0],
        )


def _first[T](value: T | None, default: T | None) -> T | None:
    return default if value is None else value


def _reasoning(extra: Mapping[str, object] | None) -> str | None:
    """vLLM and SGLang put reasoning in `reasoning_content`; newer vLLM also in `reasoning`."""
    for name in ("reasoning_content", "reasoning"):
        value = (extra or {}).get(name)
        if isinstance(value, str):
            return value
    return None


def normalize_raw(raw_json: str, requested_model: str) -> ChatResponse:
    """A recorded raw response (`GatewayRecord.upstream_response_json`), normalized again the way
    `complete` normalized it. Raises `ValueError` (pydantic's `ValidationError` is one) when it is
    not a completion with choices and token usage, which `complete` would have refused."""
    completion = ChatCompletion.model_validate_json(raw_json)
    if not completion.choices or completion.usage is None:
        raise ValueError("the raw response has no choices or no token usage")
    return normalize(completion, requested_model)


def normalize(completion: ChatCompletion, requested_model: str) -> ChatResponse:
    """The backend's response as the caller gets it. `model` stays the name the caller used;
    what the backend reported is in the record's upstream info."""
    choice = completion.choices[0]
    message = choice.message
    tool_calls = [
        WireToolCall(
            id=call.id,
            function=WireFunctionCall(name=call.function.name, arguments=call.function.arguments),
        )
        for call in message.tool_calls or ()
        if call.type == "function"
    ]
    usage = completion.usage
    assert usage is not None, "checked in Upstreams.complete"
    return ChatResponse(
        id=completion.id,
        created=completion.created,
        model=requested_model,
        choices=[
            WireChoice(
                message=WireAssistantMessage(
                    content=message.content,
                    reasoning_content=_reasoning(message.model_extra),
                    tool_calls=tool_calls or None,
                ),
                finish_reason=choice.finish_reason,
            )
        ],
        usage=WireUsage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
        ),
        system_fingerprint=completion.system_fingerprint,
    )
