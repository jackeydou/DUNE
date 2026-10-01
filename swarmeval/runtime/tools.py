"""Tool definitions, and how a tool call's arguments and output cross into and out of a tool."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from swarmeval.runtime.messages import ToolCall, ToolSchema
from swarmeval.runtime.records import EventDraft, Exec, ExecResult, ToolResult, Truncated


@dataclass(frozen=True)
class SandboxTool:
    """Runs inside the calling agent's sandbox. `build` only translates arguments to a command;
    sandboxd executes it, so its traffic crosses net-gateway like any other."""

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


Tool = SandboxTool | WorkerTool | RuntimeTool


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

BUILTIN_TOOLS: tuple[Tool, ...] = (SHELL,)
"""Tools the caller hands to `RunLoop`. The loop adds the Message Bus's `send_message` itself."""

BUILTIN_TOOL_NAMES = ("shell", "send_message")
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
