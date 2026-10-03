"""A scripted OpenAI-compatible backend. It replays responses in order and keeps every request.

Served in-process through `httpx2.ASGITransport`, so no test opens a socket to it and none calls a
live model.
"""

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_ids = itertools.count(1)


def completion(
    content: str | None = "",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning: str | None = None,
    reasoning_field: str = "reasoning_content",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    model: str = "Org/Upstream-Model",
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message[reasoning_field] = reasoning
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{next(_ids)}",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "system_fingerprint": "fp_mock",
    }


def tool_call(name: str, arguments: str, id: str = "call_x") -> dict[str, Any]:
    return {"id": id, "type": "function", "function": {"name": name, "arguments": arguments}}


@dataclass
class MockBackend:
    script: list[tuple[int, dict[str, Any]]] = field(
        default_factory=list[tuple[int, dict[str, Any]]]
    )
    """(status, body) pairs, replayed in order."""
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    respond: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    """Answers each request once the script is used up: a policy for long runs, deciding from
    what the request holds."""

    def reply(self, body: dict[str, Any], status: int = 200) -> None:
        self.script.append((status, body))

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def chat(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
            body = await request.json()
            self.requests.append(body)
            if not self.script and self.respond is not None:
                return JSONResponse(self.respond(body))
            status, reply = self.script.pop(0)
            return JSONResponse(reply, status_code=status)

        return app
