"""The worker's side of model-gateway, for one run (docs/services/model-gateway.md).

`GatewaySession` implements the runtime's `ModelClient`. It holds the run's virtual keys and its
`Attach` stream. A call goes out over HTTP; its record comes back over the stream, is committed
through the run's writer, and is acknowledged; only then does the HTTP response arrive. The
session checks that the gateway recorded the request it sent and returned the response it
recorded, since either difference means the evidence and the agent's view have split.

Everything the gateway sends is model output, so this module is also the boundary that makes it
storable: no NUL, which Postgres `jsonb` rejects.
"""

import asyncio
import hashlib
import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, Self

import grpc
import httpx2
from pydantic import JsonValue, ValidationError

from swarmeval.gateway.model.wire import (
    CALL_ID_HEADER,
    ChatRequest,
    ChatResponse,
    WireAssistantMessage,
    WireFunction,
    WireFunctionCall,
    WireMessage,
    WireSystemMessage,
    WireTool,
    WireToolCall,
    WireToolMessage,
    WireUserMessage,
)
from swarmeval.proto.swarmeval.modelgw.v1 import recorder_pb2 as pb
from swarmeval.proto.swarmeval.modelgw.v1.recorder_pb2_grpc import RecorderServiceStub
from swarmeval.runtime.messages import (
    AssistantMessage,
    ChatMessage,
    ModelRequest,
    ModelResponse,
    SystemMessage,
    ToolCall,
    ToolMessage,
    Usage,
    UserMessage,
)
from swarmeval.runtime.ports import AgentCaller, Caller, ExtensionCaller, RecordedResponse
from swarmeval.runtime.records import (
    CommittedEvent,
    EventDraft,
    GatewayRecord,
    ModelCallRecord,
    Transaction,
    Upstream,
)
from swarmeval.runtime.writer import RunWriter

UPSTREAM_ERROR_STATUS = 502
"""model-gateway's status for a call the model backend failed after retries."""


class ModelGatewayError(Exception):
    """model-gateway failed a call, or broke the recording protocol. `status` is the HTTP status
    when the gateway answered with an error; 503 means the run is not attached."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def caller_name(caller: Caller) -> str:
    match caller:
        case AgentCaller(agent_id=agent_id):
            return f"agent:{agent_id}"
        case ExtensionCaller(instance_id=instance_id):
            return f"extension:{instance_id}"


@dataclass
class _Pending:
    caller: Caller
    request: ModelRequest
    body_sha256: str
    parent_id: str | None
    done: asyncio.Future[tuple[ModelResponse, CommittedEvent, bytes]] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class GatewaySession:
    """model-gateway for one run. Use as an async context manager: entering attaches the run,
    leaving detaches it, after which the run's keys stop working."""

    def __init__(
        self,
        *,
        http: httpx2.AsyncClient,
        channel: grpc.aio.Channel,
        run_id: str,
        owner_epoch: int,
        callers: Sequence[Caller],
        writer: RunWriter,
    ) -> None:
        """`http` is based at the gateway's HTTP address and should have no read timeout: a
        call lasts as long as the model takes."""
        self._http = http
        self._stub = RecorderServiceStub(channel)
        self._run_id = run_id
        self._owner_epoch = owner_epoch
        self._keys = {caller_name(c): f"swk_{secrets.token_urlsafe(24)}" for c in callers}
        self._writer = writer
        self._pending: dict[str, _Pending] = {}
        self._calls = 0
        self._stream: grpc.aio.StreamStreamCall[pb.AttachRequest, pb.AttachResponse] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._broken: BaseException | None = None

    async def __aenter__(self) -> Self:
        stream = self._stub.Attach()
        hello = pb.Hello(
            run_id=self._run_id,
            owner_epoch=self._owner_epoch,
            keys=[pb.CallerKey(key=k, caller=c) for c, k in self._keys.items()],
        )
        try:
            await stream.write(pb.AttachRequest(hello=hello))
            first = await stream.read()
        except grpc.aio.AioRpcError as err:
            raise ModelGatewayError(
                f"run `{self._run_id}`: attaching to model-gateway failed: "
                f"{err.code().name}: {err.details()}"
            ) from err
        if not isinstance(first, pb.AttachResponse) or first.WhichOneof("item") != "attached":
            raise ModelGatewayError(
                f"run `{self._run_id}`: model-gateway did not confirm the attach."
            )
        self._stream = stream
        self._reader = asyncio.create_task(self._read(stream))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            await self._stream.done_writing()
            self._stream.cancel()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)

    async def generate(
        self, caller: Caller, request: ModelRequest, *, parent_id: str | None
    ) -> RecordedResponse:
        if self._broken is not None:
            raise ModelGatewayError(
                f"run `{self._run_id}`: the model-gateway stream failed earlier."
            ) from self._broken
        name = caller_name(caller)
        self._calls += 1
        call_id = f"call_{self._calls}"
        body = to_wire(request).model_dump_json(exclude_none=True).encode()
        pending = _Pending(caller, request, hashlib.sha256(body).hexdigest(), parent_id)
        self._pending[call_id] = pending
        headers = {
            "Authorization": f"Bearer {self._keys[name]}",
            "Content-Type": "application/json",
            CALL_ID_HEADER: call_id,
        }
        try:
            reply = await self._http.post("/v1/chat/completions", content=body, headers=headers)
        except httpx2.HTTPError as err:
            self._pending.pop(call_id, None)
            raise ModelGatewayError(
                f"run `{self._run_id}` {name} call `{call_id}`: model-gateway is unreachable: "
                f"{err!r}"
            ) from err
        if reply.status_code != 200:
            self._pending.pop(call_id, None)
            cause = pending.done.exception() if pending.done.done() else None
            raise ModelGatewayError(
                f"run `{self._run_id}` {name} call `{call_id}`: model-gateway answered "
                f"{reply.status_code}: {reply.text[:1000]}",
                reply.status_code,
            ) from cause
        response, event, recorded = await pending.done
        if reply.content != recorded:
            raise ModelGatewayError(
                f"run `{self._run_id}` {name} call `{call_id}`: the HTTP response differs from "
                f"the recorded one (event {event.event_id}). The run's evidence and the agent's "
                "view would split; stopping."
            )
        return RecordedResponse(response=response, event=event)

    async def _read(
        self, stream: grpc.aio.StreamStreamCall[pb.AttachRequest, pb.AttachResponse]
    ) -> None:
        try:
            while isinstance(message := await stream.read(), pb.AttachResponse):
                if message.WhichOneof("item") != "record":
                    raise ModelGatewayError(
                        f"run `{self._run_id}`: model-gateway sent "
                        f"`{message.WhichOneof('item')}` after the attach; only records may "
                        "follow."
                    )
                await self._commit(stream, message.record)
            raise ModelGatewayError(f"run `{self._run_id}`: model-gateway closed the stream.")
        except asyncio.CancelledError:
            raise
        except BaseException as err:
            self._broken = err
            for pending in self._pending.values():
                if not pending.done.done():
                    pending.done.set_exception(err)
            self._pending.clear()

    async def _commit(
        self,
        stream: grpc.aio.StreamStreamCall[pb.AttachRequest, pb.AttachResponse],
        record: pb.CallRecord,
    ) -> None:
        pending = self._pending.pop(record.call_id, None)
        if pending is None:
            reason = f"call `{record.call_id}` is not in flight"
            await stream.write(
                pb.AttachRequest(reject=pb.Reject(call_id=record.call_id, reason=reason))
            )
            raise ModelGatewayError(
                f"run `{self._run_id}`: model-gateway sent a record for {reason}."
            )
        try:
            model_record = self._model_record(pending, record)
            draft = EventDraft(
                record=model_record,
                agent_id=pending.caller.agent_id
                if isinstance(pending.caller, AgentCaller)
                else None,
                extension=(
                    pending.caller.instance_id
                    if isinstance(pending.caller, ExtensionCaller)
                    else None
                ),
                parent_id=pending.parent_id,
            )
            (event,) = await self._writer.commit(Transaction(events=[draft]))
        except BaseException as err:
            # Before the reject goes out, so the caller's 503 can name this cause.
            self._broken = err
            pending.done.set_exception(err)
            reject = pb.Reject(call_id=record.call_id, reason=f"not committed: {err}")
            await stream.write(pb.AttachRequest(reject=reject))
            raise
        await stream.write(
            pb.AttachRequest(ack=pb.Ack(call_id=record.call_id, event_id=event.event_id))
        )
        response = ModelResponse(message=model_record.response, usage=model_record.usage)
        pending.done.set_result((response, event, record.response_json.encode()))

    def _model_record(self, pending: _Pending, record: pb.CallRecord) -> ModelCallRecord:
        where = f"run `{self._run_id}` call `{record.call_id}`"
        if record.request_sha256 != pending.body_sha256:
            raise ModelGatewayError(
                f"{where}: model-gateway recorded a request with sha256 {record.request_sha256}, "
                f"but the worker sent {pending.body_sha256}."
            )
        try:
            wire = ChatResponse.model_validate_json(record.response_json)
            sampling = json.loads(record.upstream.sampling_json)
        except (ValidationError, ValueError) as err:
            raise ModelGatewayError(
                f"{where}: unreadable record from model-gateway: {err}"
            ) from err
        response = from_wire(wire)
        upstream = record.upstream
        request = pending.request
        return ModelCallRecord(
            model=request.model,
            gen=request.gen,
            length=None if request.gen is None else len(request.messages),
            options=request.options,
            response=response.message,
            usage=response.usage,
            gateway=GatewayRecord(
                request_sha256=record.request_sha256,
                upstream=Upstream(
                    backend=upstream.backend,
                    model=upstream.model,
                    served_model=_clean(upstream.served_model),
                    reasoning_passback=_passback(upstream.reasoning_passback, where),
                    weights_hash=upstream.weights_hash or None,
                    system_fingerprint=_clean(upstream.system_fingerprint) or None,
                    sampling=_object(sampling, where),
                    reasoning_visibility="full",
                ),
                upstream_response_json=_clean(record.upstream_response_json),
                latency_s=record.latency_seconds,
                attempts=record.attempts,
            ),
        )


def _clean(text: str) -> str:
    return text.replace("\x00", "�")


def _passback(value: str, where: str) -> Literal["none", "within_turn", "all"]:
    match value:
        case "none" | "within_turn" | "all":
            return value
        case _:
            raise ModelGatewayError(f"{where}: unknown reasoning_passback {value!r}.")


def _object(value: JsonValue, where: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ModelGatewayError(f"{where}: sampling must be a JSON object, got {value!r}.")
    return value


def _wire_message(message: ChatMessage) -> WireMessage:
    match message:
        case SystemMessage(content=content):
            return WireSystemMessage(content=content)
        case UserMessage(content=content):
            return WireUserMessage(content=content)
        case AssistantMessage():
            calls = [
                WireToolCall(id=c.id, function=WireFunctionCall(name=c.name, arguments=c.arguments))
                for c in message.tool_calls
            ]
            return WireAssistantMessage(
                content=message.content if message.content or not calls else None,
                reasoning_content=message.reasoning,
                tool_calls=calls or None,
            )
        case ToolMessage(tool_call_id=tool_call_id, content=content):
            return WireToolMessage(tool_call_id=tool_call_id, content=content)


def to_wire(request: ModelRequest) -> ChatRequest:
    options = request.options
    return ChatRequest(
        model=request.model,
        messages=[_wire_message(m) for m in request.messages],
        tools=[
            WireTool(
                function=WireFunction(
                    name=t.name, description=t.description, parameters=t.parameters
                )
            )
            for t in request.tools
        ]
        or None,
        temperature=options.temperature,
        top_p=options.top_p,
        max_completion_tokens=options.max_output_tokens,
        seed=options.seed,
    )


def from_wire(response: ChatResponse) -> ModelResponse:
    message = response.choices[0].message
    reasoning = message.reasoning_content
    return ModelResponse(
        message=AssistantMessage(
            content=_clean(message.content or ""),
            reasoning=None if reasoning is None else _clean(reasoning),
            tool_calls=tuple(
                ToolCall(
                    id=_clean(c.id),
                    name=_clean(c.function.name),
                    arguments=_clean(c.function.arguments),
                )
                for c in message.tool_calls or ()
            ),
        ),
        usage=Usage(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        ),
    )
