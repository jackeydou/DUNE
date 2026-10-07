"""Running one tool call: argument validation, then the runtime tool, the worker tool, the web
request, or the sandbox command it names (docs/agent-runtime.md#tools)."""

from dataclasses import dataclass, replace

from swarmeval.runtime.extensions.api import AgentInfo, ExtensionError
from swarmeval.runtime.extensions.dispatch import HookDispatcher
from swarmeval.runtime.messages import ToolCall
from swarmeval.runtime.ports import SandboxExecutor, WebClient
from swarmeval.runtime.records import ExecResult, ToolResult, Transaction, WebExchange
from swarmeval.runtime.specs import AgentSpec
from swarmeval.runtime.tools import (
    RuntimeTool,
    Tool,
    WebTool,
    WorkerTool,
    exec_output,
    parse_arguments,
    web_output,
)


@dataclass(frozen=True)
class Execution:
    """A tool call's outcome. `executed` is the arguments that actually ran, `None` when nothing
    did; `exec_result` and `web` are what sandboxd or the web client observed."""

    result: ToolResult
    executed: str | None = None
    exec_result: ExecResult | None = None
    web: WebExchange | None = None


class ToolRunner:
    """`tools` is every tool of the run by name; the loop checks agents' tool lists against it
    at construction."""

    def __init__(
        self, tools: dict[str, Tool], sandbox: SandboxExecutor, web: WebClient | None
    ) -> None:
        self.tools = tools
        self._sandbox = sandbox
        self._web = web

    async def execute(
        self,
        d: HookDispatcher,
        agent: AgentSpec,
        info: AgentInfo,
        call: ToolCall,
        txn: Transaction,
        offered: tuple[str, ...],
        model_event_id: str,
    ) -> Execution:
        """`offered` is the tool list of the request that produced the call, after
        `before_model_request` narrowed it. A call outside it is refused even if the agent
        otherwise has the tool. Events the tool causes name `model_event_id` as their parent."""
        tool = self.tools.get(call.name) if call.name in offered else None
        if tool is None:
            available = ", ".join(offered) or "none"
            content = f"Unknown tool `{call.name}`. Available tools: {available}."
            return Execution(
                ToolResult(call_id=call.id, tool=call.name, content=content, is_error=True)
            )
        parsed = parse_arguments(tool, call)
        if isinstance(parsed, ToolResult):
            return Execution(parsed)
        if isinstance(tool, RuntimeTool):
            outcome = tool.run(parsed, agent.id, call.id)
            txn.events.extend(replace(e, parent_id=model_event_id) for e in outcome.events)
            result = ToolResult(
                call_id=call.id, tool=call.name, content=outcome.content, is_error=outcome.is_error
            )
            return Execution(result, call.arguments)
        if isinstance(tool, WorkerTool):
            content, effects = await d.run_worker_tool(tool, info, parsed, model_event_id)
            txn.extend(effects)
            return Execution(
                ToolResult(call_id=call.id, tool=call.name, content=content), call.arguments
            )
        if isinstance(tool, WebTool):
            assert self._web is not None, "checked in _check_tools"
            exchange = await self._web.request(tool.build(parsed))
            return Execution(web_output(call, exchange), call.arguments, web=exchange)
        try:
            command = tool.build(parsed)
        except Exception as err:
            if tool.owner is None:
                raise
            raise ExtensionError(
                tool.owner, "tool", f"building `{tool.name}` failed: {err}"
            ) from err
        assert agent.sandbox_id is not None, "checked in _check_tools"
        result = await self._sandbox.exec(agent.sandbox_id, agent.os_user, command, call_id=call.id)
        output = tool.output or exec_output
        return Execution(output(call, result), call.arguments, exec_result=result)
