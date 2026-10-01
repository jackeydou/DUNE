"""The agent loop: one run, its agents taking turns, every step recorded before it is admitted.

A step is one model call plus every tool call it makes. Content reaches an agent's context in
two commits: first as observed (record), then as the agent will see it after hooks (admit).
See docs/agent-runtime.md.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from swarmeval.gateway.bus import ChannelSpec, MessageBus
from swarmeval.runtime.extensions.api import (
    AgentInfo,
    Block,
    CanaryInfo,
    ExtensionError,
    Inject,
    Rewrite,
    RunInfo,
    Skip,
    Stop,
)
from swarmeval.runtime.extensions.dispatch import HookDispatcher
from swarmeval.runtime.extensions.registry import LoadedExtension
from swarmeval.runtime.messages import (
    ChatMessage,
    ModelRequest,
    RequestOptions,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from swarmeval.runtime.ports import AgentCaller, ModelClient, SandboxExecutor
from swarmeval.runtime.records import (
    AgentStateRow,
    EventDraft,
    ExecResult,
    LifecycleRecord,
    LimitRecord,
    ToolCallRecord,
    ToolResult,
    Transaction,
)
from swarmeval.runtime.tools import (
    RuntimeTool,
    SandboxTool,
    Tool,
    WorkerTool,
    exec_output,
    parse_arguments,
    tool_schema,
)
from swarmeval.runtime.writer import RunWriter


class RunConfigError(Exception):
    """The run's agents and tools do not fit together."""


@dataclass(frozen=True)
class AgentSpec:
    id: str
    model: str
    system_prompt: str
    task: str
    tools: tuple[str, ...] = ()
    sandbox_id: str | None = None
    os_user: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    seed: int | None = None


@dataclass(frozen=True)
class Limits:
    max_turns: int | None = None
    """Turns across all agents."""
    max_tokens: int | None = None
    """Tokens across all agents. Checked before each model call, so one call may overshoot."""


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    seed: int
    agents: tuple[AgentSpec, ...]
    limits: Limits = Limits()
    channels: tuple[ChannelSpec, ...] = ()
    canaries: tuple[CanaryInfo, ...] = ()


@dataclass(frozen=True)
class RunOutcome:
    status: Literal["finished", "stopped", "limit"]
    reason: str | None
    turns: int
    tokens_used: int


class _AgentRun:
    def __init__(self, spec: AgentSpec) -> None:
        self.spec = spec
        self.info = AgentInfo(
            id=spec.id, model=spec.model, sandbox_id=spec.sandbox_id, tools=spec.tools
        )
        self.gen = 0
        self.length = 0
        self.turn = 0
        self.finished = False


class _Stopped(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


class RunLoop:
    """Drives one run. `writer` is the run's only writer, shared with the model-gateway stream
    handler so that model records and loop commits land in one sequence."""

    def __init__(
        self,
        spec: RunSpec,
        *,
        writer: RunWriter,
        model_client: ModelClient,
        sandbox_executor: SandboxExecutor,
        extensions: Sequence[LoadedExtension] = (),
        tools: Sequence[Tool] = (),
        hook_timeout_s: float = 30.0,
    ) -> None:
        self._spec = spec
        self._writer = writer
        self._store = writer.store
        self._model = model_client
        self._sandbox = sandbox_executor
        self._bus = MessageBus(spec.channels)
        writer.subscribe(self._bus.on_commit)
        bus_tool = self._bus.tool()
        self._tools: dict[str, Tool] = {t.name: t for t in tools}
        self._tools[bus_tool.name] = bus_tool
        for ext in extensions:
            self._tools.update({t.name: t for t in ext.registrations.tools})
        self._agents = {a.id: _AgentRun(a) for a in spec.agents}
        self._check_tools()
        self._tokens_used = 0
        self._turns = 0
        self._extensions = extensions
        self._hook_timeout_s = hook_timeout_s
        self._stop_reason: str | None = None

    def _check_tools(self) -> None:
        for agent in self._spec.agents:
            for name in agent.tools:
                tool = self._tools.get(name)
                if tool is None:
                    raise RunConfigError(
                        f"agent `{agent.id}` lists tool `{name}`, which neither the runtime nor "
                        f"any loaded extension provides. Known tools: "
                        f"{', '.join(sorted(self._tools)) or 'none'}."
                    )
                if isinstance(tool, SandboxTool) and agent.sandbox_id is None:
                    raise RunConfigError(
                        f"agent `{agent.id}` lists sandbox tool `{name}` but has no sandbox. "
                        "Give it `sandbox:` or `sandbox_profile:`, or drop the tool."
                    )

    async def run(self) -> RunOutcome:
        """Runs a fresh run to its end. Raises `ExtensionError` after recording the failure."""
        dispatcher = HookDispatcher(
            extensions=self._extensions,
            run=RunInfo(
                run_id=self._spec.run_id,
                agent_ids=tuple(self._agents),
                canaries=self._spec.canaries,
            ),
            agents={a.spec.id: a.info for a in self._agents.values()},
            seed=self._spec.seed,
            states=await self._store.extension_states(),
            model_client=self._model,
            sandbox_executor=self._sandbox,
            writer=self._writer,
            default_timeout_s=self._hook_timeout_s,
        )
        dispatcher.start()
        try:
            await self._start()
            await dispatcher.observe("on_run_start", None)
            outcome = await self._drive(dispatcher)
            await dispatcher.observe("on_run_end", None)
            await dispatcher.settle()
            record = LifecycleRecord(status=outcome.status, reason=outcome.reason)
            await self._writer.commit(Transaction(events=[EventDraft(record=record)]))
            await dispatcher.settle()
            return outcome
        except ExtensionError as err:
            dispatcher.fail(err)
            record = LifecycleRecord(
                status="failed", hook=err.hook, error=f"{err} (cause: {err.__cause__!r})"
            )
            draft = EventDraft(record=record, extension=err.instance_id)
            await self._writer.commit(Transaction(events=[draft]))
            raise
        finally:
            await dispatcher.close()

    async def _start(self) -> None:
        txn = Transaction(events=[EventDraft(record=LifecycleRecord(status="started"))])
        for agent in self._agents.values():
            if await self._store.context(agent.spec.id) is not None:
                raise RunConfigError(
                    f"run `{self._spec.run_id}` already has context for agent `{agent.spec.id}`. "
                    "Resuming a run is not supported yet; start a new run id."
                )
            initial: tuple[ChatMessage, ...] = (
                SystemMessage(content=agent.spec.system_prompt),
                UserMessage(content=agent.spec.task),
            )
            txn.new_generations[agent.spec.id] = initial
            agent.length = len(initial)
            txn.agent_states.append(self._state_row(agent, "ready"))
        await self._writer.commit(txn)

    async def _drive(self, dispatcher: HookDispatcher) -> RunOutcome:
        limits = self._spec.limits
        try:
            while True:
                for agent in self._agents.values():
                    if agent.finished and self._bus.has_mail(agent.spec.id):
                        # Mail wakes a finished agent; admitting it records the state change.
                        agent.finished = False
                active = [a for a in self._agents.values() if not a.finished]
                if not active:
                    return self._outcome("finished", None)
                for agent in active:
                    self._check_stop(dispatcher)
                    if limits.max_turns is not None and self._turns >= limits.max_turns:
                        return await self._limit("max_turns", limits.max_turns)
                    if limits.max_tokens is not None and self._tokens_used >= limits.max_tokens:
                        return await self._limit("max_tokens", limits.max_tokens)
                    self._turns += 1
                    await self._step(dispatcher, agent)
        except _Stopped as stop:
            return self._outcome("stopped", stop.reason)

    def stop(self, reason: str) -> None:
        """Asks the run to stop, from outside the loop (a cancel). Takes effect at the next hook
        point, like a stop an extension requests."""
        if self._stop_reason is None:
            self._stop_reason = reason

    def _stop_requested(self, dispatcher: HookDispatcher) -> str | None:
        return self._stop_reason or dispatcher.stop_reason

    def _check_stop(self, dispatcher: HookDispatcher) -> None:
        reason = self._stop_requested(dispatcher)
        if reason is not None:
            raise _Stopped(reason)

    async def _limit(self, limit: Literal["max_turns", "max_tokens"], value: int) -> RunOutcome:
        record = LimitRecord(limit=limit, value=value)
        await self._writer.commit(Transaction(events=[EventDraft(record=record)]))
        return self._outcome("limit", f"{limit} reached ({value})")

    def _outcome(
        self, status: Literal["finished", "stopped", "limit"], reason: str | None
    ) -> RunOutcome:
        return RunOutcome(
            status=status, reason=reason, turns=self._turns, tokens_used=self._tokens_used
        )

    async def _step(self, d: HookDispatcher, agent: _AgentRun) -> None:
        agent.turn += 1
        gate = await d.before_turn(agent.info, self._turns)
        txn = gate.txn
        deliveries = self._bus.take(agent.spec.id)
        txn.events.extend(delivery.draft for delivery in deliveries)
        admitted: list[ChatMessage] = [delivery.message for delivery in deliveries]
        admitted.extend(d.take_injections(agent.spec.id))
        if isinstance(gate.decision, Inject):
            admitted.extend(gate.decision.messages)
        self._admit(txn, agent, admitted)
        await self._writer.commit(txn)
        match gate.decision:
            case Stop(reason=reason):
                raise _Stopped(f"{gate.decided_by}: {reason}")
            case Skip():
                return
            case _:
                pass
        self._check_stop(d)

        context = await self._store.context(agent.spec.id)
        assert context is not None, "every agent gets generation 0 in _start"
        messages = context.messages
        compacted = await d.compact_context(agent.info, messages)
        txn = compacted.txn
        if compacted.value is not None:
            messages = compacted.value
            agent.gen += 1
            agent.length = len(messages)
            txn.new_generations[agent.spec.id] = messages
            txn.agent_states.append(self._state_row(agent, "ready"))
        await self._writer.commit(txn)
        self._check_stop(d)

        spec = agent.spec
        options = RequestOptions(
            tools=spec.tools,
            temperature=spec.temperature,
            top_p=spec.top_p,
            max_output_tokens=spec.max_output_tokens,
            seed=spec.seed,
        )
        requested = await d.before_model_request(agent.info, options)
        await self._writer.commit(requested.txn)
        self._check_stop(d)
        request = ModelRequest(
            model=spec.model,
            messages=messages,
            gen=agent.gen,
            tools=tuple(tool_schema(self._tools[name]) for name in requested.value.tools),
            options=requested.value,
        )
        recorded = await self._model.generate(AgentCaller(spec.id), request)
        self._tokens_used += recorded.response.usage.total
        model_event_id = recorded.event.event_id

        response = await d.after_model_response(
            agent.info, recorded.response.message, model_event_id
        )
        txn = response.txn
        self._admit(txn, agent, [response.value])
        await self._writer.commit(txn)

        for call in response.value.tool_calls:
            await self._tool_step(d, agent, call, model_event_id, requested.value.tools)
        if not response.value.tool_calls:
            agent.finished = True
            txn = Transaction(agent_states=[self._state_row(agent, "finished")])
            await self._writer.commit(txn)
        await d.observe("after_turn", agent.info)

    async def _tool_step(
        self,
        d: HookDispatcher,
        agent: _AgentRun,
        call: ToolCall,
        model_event_id: str,
        offered: tuple[str, ...],
    ) -> None:
        gate = await d.before_tool_call(agent.info, call, model_event_id)
        txn = gate.txn
        reason = self._stop_requested(d)
        if reason is not None:
            await self._writer.commit(txn)
            raise _Stopped(reason)
        executed: str | None = None
        exec_result: ExecResult | None = None
        blocked_by: str | None = None
        match gate.decision:
            case Block(result=content, is_error=is_error):
                result = ToolResult(
                    call_id=call.id, tool=call.name, content=content, is_error=is_error
                )
                blocked_by = gate.decided_by
            case decision:
                arguments = (
                    json.dumps(decision.arguments)
                    if isinstance(decision, Rewrite)
                    else call.arguments
                )
                effective = call.model_copy(update={"arguments": arguments})
                result, executed, exec_result = await self._execute(
                    d, agent, effective, txn, offered
                )

        record = ToolCallRecord(
            call=call,
            executed_arguments=executed,
            result=result,
            blocked_by=blocked_by,
            exec_result=exec_result,
        )
        draft = EventDraft(record=record, agent_id=agent.spec.id, parent_id=model_event_id)
        txn.events.append(draft)
        committed = await self._writer.commit(txn)
        tool_event = next(e for e in committed if isinstance(e.record, ToolCallRecord))

        admitted = await d.after_tool_result(agent.info, result, tool_event.event_id)
        txn = admitted.txn
        message = ToolMessage(
            tool_call_id=call.id, content=admitted.value.content, is_error=admitted.value.is_error
        )
        self._admit(txn, agent, [message])
        await self._writer.commit(txn)

    async def _execute(
        self,
        d: HookDispatcher,
        agent: _AgentRun,
        call: ToolCall,
        txn: Transaction,
        offered: tuple[str, ...],
    ) -> tuple[ToolResult, str | None, ExecResult | None]:
        """`offered` is the tool list of the request that produced the call, after
        `before_model_request` narrowed it. A call outside it is refused even if the agent
        otherwise has the tool."""
        tool = self._tools.get(call.name) if call.name in offered else None
        if tool is None:
            available = ", ".join(offered) or "none"
            content = f"Unknown tool `{call.name}`. Available tools: {available}."
            return (
                ToolResult(call_id=call.id, tool=call.name, content=content, is_error=True),
                None,
                None,
            )
        parsed = parse_arguments(tool, call)
        if isinstance(parsed, ToolResult):
            return parsed, None, None
        if isinstance(tool, RuntimeTool):
            outcome = tool.run(parsed, agent.spec.id, call.id)
            txn.events.extend(outcome.events)
            result = ToolResult(
                call_id=call.id, tool=call.name, content=outcome.content, is_error=outcome.is_error
            )
            return result, call.arguments, None
        if isinstance(tool, WorkerTool):
            content, effects = await d.run_worker_tool(tool, agent.info, parsed)
            txn.extend(effects)
            return (
                ToolResult(call_id=call.id, tool=call.name, content=content),
                call.arguments,
                None,
            )
        try:
            command = tool.build(parsed)
        except Exception as err:
            if tool.owner is None:
                raise
            raise ExtensionError(
                tool.owner, "tool", f"building `{tool.name}` failed: {err}"
            ) from err
        assert agent.spec.sandbox_id is not None, "checked in _check_tools"
        result = await self._sandbox.exec(
            agent.spec.sandbox_id, agent.spec.os_user, command, call_id=call.id
        )
        return exec_output(call, result), call.arguments, result

    def _admit(self, txn: Transaction, agent: _AgentRun, messages: Sequence[ChatMessage]) -> None:
        if not messages:
            return
        txn.messages.extend((agent.spec.id, m) for m in messages)
        agent.length += len(messages)
        txn.agent_states.append(self._state_row(agent, "ready"))

    def _state_row(
        self, agent: _AgentRun, status: Literal["awaiting_admit", "ready", "finished"]
    ) -> AgentStateRow:
        return AgentStateRow(
            agent_id=agent.spec.id,
            gen=agent.gen,
            length=agent.length,
            turn=agent.turn,
            status=status,
            tokens_used=self._tokens_used,
        )
