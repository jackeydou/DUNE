"""Tool definitions, and how a tool call's arguments and output cross into and out of a tool."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from swarmeval.runtime.messages import ToolCall, ToolSchema
from swarmeval.runtime.records import (
    EventDraft,
    Exec,
    ExecResult,
    ToolResult,
    Truncated,
    WebExchange,
    WebRequest,
)


@dataclass(frozen=True)
class SandboxTool:
    """Runs inside the calling agent's sandbox. `build` only translates arguments to a command;
    sandboxd executes it, so it has only the sandbox's network, which reaches nothing."""

    name: str
    description: str
    args: type[BaseModel]
    build: Callable[[Any], Exec]
    owner: str | None = None


@dataclass(frozen=True)
class WorkerTool:
    """Runs in the worker with only its extension's `HookContext`. Must not do its own I/O."""

    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[Any, Any], Awaitable[str]]
    owner: str


@dataclass(frozen=True)
class RuntimeOutcome:
    content: str
    is_error: bool = False
    events: tuple[EventDraft, ...] = ()
    """Committed in the same transaction as the tool call."""


@dataclass(frozen=True)
class RuntimeTool:
    """Provided by the runtime itself and run in the worker, such as the Message Bus's
    `send_message`. `run` receives the parsed arguments, the calling agent, and the call id, and
    does no I/O: whatever it causes is returned as events."""

    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[Any, str, str], RuntimeOutcome]


@dataclass(frozen=True)
class WebTool:
    """Runs in the worker through the run's `WebClient`, never in a sandbox, which has no network.
    `build` only translates arguments to a request."""

    name: str
    description: str
    args: type[BaseModel]
    build: Callable[[Any], WebRequest]


Tool = SandboxTool | WorkerTool | RuntimeTool | WebTool


def tool_schema(tool: Tool) -> ToolSchema:
    return ToolSchema(
        name=tool.name, description=tool.description, parameters=tool.args.model_json_schema()
    )


def parse_arguments(tool: Tool, call: ToolCall) -> BaseModel | ToolResult:
    """Model output is untrusted input: bad arguments become an error result the agent sees."""
    try:
        return tool.args.model_validate_json(call.arguments)
    except ValidationError as err:
        return ToolResult(
            call_id=call.id,
            tool=call.name,
            content=f"Invalid arguments for `{call.name}`: {err}",
            is_error=True,
        )


class ShellArgs(BaseModel):
    cmd: str = Field(description="Command line, run with `sh -c`.")
    timeout_s: float = Field(
        default=60.0, gt=0, le=600, description="Seconds before the command is killed."
    )


def _build_shell(args: ShellArgs) -> Exec:
    return Exec(argv=("sh", "-c", args.cmd), timeout_s=args.timeout_s)


SHELL = SandboxTool(
    name="shell",
    description="Run a shell command in your sandbox. Returns stdout, then stderr.",
    args=ShellArgs,
    build=_build_shell,
)

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HEADER_VALUE = re.compile(r"[\t\x20-\x7e\x80-\xff]*")


class WebRequestArgs(BaseModel):
    url: str = Field(max_length=8192, description="Absolute http:// or https:// URL.")
    method: str = Field(
        default="GET", pattern=r"^[A-Z]{1,20}$", description="HTTP method, in upper case."
    )
    headers: dict[str, str] = Field(default_factory=dict[str, str], description="Request headers.")
    body: str | None = Field(default=None, description="Request body, sent as UTF-8.")
    timeout_s: float = Field(
        default=30.0, gt=0, le=120, description="Seconds before the request is abandoned."
    )

    @field_validator("headers")
    @classmethod
    def _headers(cls, headers: dict[str, str]) -> dict[str, str]:
        for name, value in headers.items():
            if not _HEADER_NAME.fullmatch(name):
                raise ValueError(f"header name {name!r} is not an HTTP token")
            if name.lower() == "host":
                raise ValueError("`Host` is set from the URL; leave it out of `headers`")
            if not _HEADER_VALUE.fullmatch(value):
                raise ValueError(
                    f"header `{name}` has control characters or text outside Latin-1 in its value"
                )
        return headers


def _build_web_request(args: WebRequestArgs) -> WebRequest:
    return WebRequest(
        method=args.method,
        url=args.url,
        headers=tuple(args.headers.items()),
        body=args.body,
        timeout_s=args.timeout_s,
    )


WEB_REQUEST = WebTool(
    name="web_request",
    description=(
        "Send an HTTP request to the internet and get the response: status line, headers, then "
        "the body, cut off if it is long. Redirects are not followed; request the `location` "
        "yourself."
    ),
    args=WebRequestArgs,
    build=_build_web_request,
)

BUILTIN_TOOLS: tuple[Tool, ...] = (SHELL, WEB_REQUEST)
"""Tools the caller hands to `RunLoop`. The loop adds the Message Bus's `send_message` itself."""

BUILTIN_TOOL_NAMES = ("shell", "send_message", "web_request")
"""Every tool name the runtime provides. Extensions may not reuse them."""


def _truncation(stream: str, truncated: Truncated | None) -> str:
    if truncated is None:
        return ""
    return f"\n[{stream} truncated: the command wrote {truncated.size} bytes]\n"


def exec_output(call: ToolCall, result: ExecResult) -> ToolResult:
    content = (
        result.stdout
        + _truncation("stdout", result.stdout_truncated)
        + result.stderr
        + _truncation("stderr", result.stderr_truncated)
    )
    if result.timed_out:
        content += "\n[command timed out]"
    elif result.exit_code != 0:
        content += f"\n[exit code {result.exit_code}]"
    return ToolResult(
        call_id=call.id,
        tool=call.name,
        content=content,
        is_error=result.timed_out or result.exit_code != 0,
    )


def web_output(call: ToolCall, exchange: WebExchange) -> ToolResult:
    """A refused request reads like a refused connection: why it was refused stays in the
    record, out of the agent's view."""
    if exchange.refused is not None:
        content = f"Could not connect to {exchange.request.url}: connection refused."
        return ToolResult(call_id=call.id, tool=call.name, content=content, is_error=True)
    if exchange.error is not None:
        content = f"Request to {exchange.request.url} failed: {exchange.error}"
        return ToolResult(call_id=call.id, tool=call.name, content=content, is_error=True)
    lines = [f"HTTP {exchange.status}"]
    lines.extend(f"{name}: {value}" for name, value in exchange.response_headers)
    content = "\n".join(lines) + "\n\n" + exchange.body
    if exchange.response_body_capped or exchange.response_body_size > exchange.body_bytes:
        size = exchange.response_body_size
        total = f"over {size}" if exchange.response_body_capped else str(size)
        content += f"\n[body truncated: showing {exchange.body_bytes} of {total} bytes]"
    return ToolResult(call_id=call.id, tool=call.name, content=content)
