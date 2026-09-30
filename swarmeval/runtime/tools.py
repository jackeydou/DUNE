"""Tool definitions, and how a tool call's arguments and output cross into and out of a tool."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from swarmeval.runtime.messages import ToolCall, ToolSchema
from swarmeval.runtime.records import Exec, ExecResult, ToolResult


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


Tool = SandboxTool | WorkerTool


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


def exec_output(call: ToolCall, result: ExecResult) -> ToolResult:
    content = result.stdout + result.stderr
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
