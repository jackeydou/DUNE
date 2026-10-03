"""The agent loop: one run, its agents taking turns, every step recorded before it is admitted.

A step is one model call plus every tool call it makes. Content reaches an agent's context in
two commits: first as observed (record), then as the agent will see it after hooks (admit).
See docs/agent-runtime.md.
"""

import asyncio
import json
from collections.abc import Sequence
from typing import Literal

from swarmeval.gateway.bus import MessageBus
from swarmeval.runtime.concurrency import AsyncDriver
from swarmeval.runtime.execute import Execution, ToolRunner
from swarmeval.runtime.extensions.api import (
    Block,
    ChannelInfo,
    Envelope,
    ExtensionError,
    Inject,
    ResumeInfo,
    Rewrite,
    RunInfo,
    Skip,
    Stop,
)
from swarmeval.runtime.extensions.dispatch import HookDispatcher, PauseRequest
from swarmeval.runtime.extensions.registry import LoadedExtension
from swarmeval.runtime.fork import (
    ForkStart,
)
from swarmeval.runtime.messages import (
    ChatMessage,
    ModelRequest,
    RequestOptions,
    ToolCall,
    ToolMessage,
    ToolSchema,
)
from swarmeval.runtime.ports import AgentCaller, ModelClient, Pauser, SandboxExecutor, WebClient
from swarmeval.runtime.records import (
    DeliveryChange,
    EventDraft,
    ExtensionSnapshot,
    LifecycleRecord,
    LimitRecord,
    ToolCallRecord,
    ToolResult,
    Transaction,
)
from swarmeval.runtime.specs import (
    RunConfigError,
    RunOutcome,
    RunSpec,
    initial_context,
)
from swarmeval.runtime.state import AgentRun, Stopped, checkpoint_of, fork_start
from swarmeval.runtime.tools import (
    SandboxTool,
    Tool,
    WebTool,
    tool_schema,
)
from swarmeval.runtime.writer import RunWriter


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
        pauser: Pauser,
        extensions: Sequence[LoadedExtension] = (),
        tools: Sequence[Tool] = (),
        web_client: WebClient | None = None,
        hook_timeout_s: float = 30.0,
        fork: ForkStart | None = None,
    ) -> None:
        """`fork` starts the run from another run's checkpoint instead of from the case's
        prompts."""
        self._spec = spec
        self._writer = writer
        self._store = writer.store
        self._model = model_client
        self._web = web_client
        self._fork = fork
        self._pauser = pauser
        self._bus = MessageBus(spec.channels)
        self._resumed = asyncio.Event()
        """Cleared while the run is paused: every agent's next hook point waits on it."""
        self._resumed.set()
        self._paused_s = 0.0
        self._open_pauses = 0
        """Pauses not yet resumed; under `async` one agent can pause while another is."""
        self._pause_began = 0.0
        self._limit_claim: asyncio.Future[tuple[RunOutcome, str]] | None = None
        """The run-wide limit being recorded, which every agent that sees it awaits."""
        self._clock_start = 0.0
        self._driver: AsyncDriver | None = None
        self._poke: asyncio.Task[None] | None = None
        """Wakes the `async` driver's waiting agents after a cancel; held so it is not lost."""
        writer.subscribe(self._bus.on_commit)
        bus_tool = self._bus.tool()
        self._tools: dict[str, Tool] = {t.name: t for t in tools}
        self._tools[bus_tool.name] = bus_tool
        for ext in extensions:
            self._tools.update({t.name: t for t in ext.registrations.tools})
        self._agents = {a.id: AgentRun(a) for a in spec.agents}
        self._check_tools()
        self._runner = ToolRunner(self._tools, sandbox_executor, web_client)
        self._sandbox = sandbox_executor
        self._tokens_used = 0
        self._turns = 0
        self._extensions = extensions
        self._hook_timeout_s = hook_timeout_s
        self._stop_reason: str | None = None
        self._started_id = ""
        """The `started` lifecycle event: what run-level hooks and the run's last lifecycle
        event descend from. Set by `_start`."""
        self._end_cause: str | None = None
        """The event that ended the run (a limit, a stop), when one did."""

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
                if isinstance(tool, WebTool) and self._web is None:
                    raise RunConfigError(
                        f"agent `{agent.id}` lists `{name}`, but the run was started without a "
                        "web client to send its requests."
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
                sandbox_canaries=self._spec.sandbox_canaries,
                channels=tuple(
                    ChannelInfo(id=c.id, members=c.members) for c in self._spec.channels
                ),
                agents=tuple(a.info for a in self._agents.values()),
            ),
            agents={a.spec.id: a.info for a in self._agents.values()},
            seed=self._spec.seed,
            states=(
                {
                    k: ExtensionSnapshot(v.state, v.rng_uses)
                    for k, v in fork.checkpoint.extensions.items()
                }
                if (fork := self._fork) is not None
                else await self._store.extension_states()
            ),
            model_client=self._model,
            sandbox_executor=self._sandbox,
            writer=self._writer,
            default_timeout_s=self._hook_timeout_s,
        )
        dispatcher.timed_delays = self._spec.turn_policy == "async"
        dispatcher.turn_delays = self._spec.turn_policy != "async"
        dispatcher.quiesce = self._spec.turn_policy != "async"
        dispatcher.start()
        self._clock_start = asyncio.get_running_loop().time()
        try:
            if self._fork is None:
                await self._start()
                await dispatcher.observe("on_run_start", None, self._started_id)
                outcome = await self._drive(dispatcher)
            else:
                await self._start_fork(dispatcher, self._fork)
                info = ResumeInfo(
                    fork=True,
                    source_run_id=self._fork.source_run_id,
                    at_seq=self._fork.fork_seq,
                    fidelity=self._fork.fidelity,
                )
                await dispatcher.resume(info, self._started_id)
                outcome = await self._drive(dispatcher, self._fork.checkpoint.round)
            dispatcher.quiesce = True
            await dispatcher.observe("on_run_end", None, self._started_id)
            await dispatcher.settle()
            record = LifecycleRecord(status=outcome.status, reason=outcome.reason)
            draft = EventDraft(record=record, parent_id=self._end_cause or self._started_id)
            await self._writer.commit(Transaction(events=[draft]))
            await dispatcher.settle()
            return outcome
        except ExtensionError as err:
            dispatcher.fail(err)
            record = LifecycleRecord(
                status="failed", hook=err.hook, error=f"{err} (cause: {err.__cause__!r})"
            )
            draft = EventDraft(
                record=record,
                extension=err.instance_id,
                parent_id=self._started_id or self._writer.last_event_id,
            )
            await self._writer.commit(Transaction(events=[draft]))
            raise
        finally:
            await dispatcher.close()

    async def _start(self) -> None:
        started = EventDraft(
            record=LifecycleRecord(status="started"), parent_id=self._writer.last_event_id
        )
        txn = Transaction(events=[started])
        for agent in self._agents.values():
            if await self._store.context(agent.spec.id) is not None:
                raise RunConfigError(
                    f"run `{self._spec.run_id}` already has context for agent `{agent.spec.id}`. "
                    "Resuming a run is not supported yet; start a new run id."
                )
            initial = initial_context(agent.spec)
            txn.new_generations[agent.spec.id] = initial
            agent.length = len(initial)
            agent.last_input = started.event_id
            txn.agent_states.append(agent.row("ready", self._tokens_used))
        await self._writer.commit(txn)
        self._started_id = started.event_id

    async def _drive(self, dispatcher: HookDispatcher, resume: Sequence[str] = ()) -> RunOutcome:
        """`resume` is a fork's round in progress: the agents left in it. Under `event_driven`
        an agent, once it steps, goes on until it answers without a tool call; under
        `round_robin` it takes one step per round."""
        if self._spec.turn_policy == "async":
            assert not resume, "forks of async runs are refused: they have no checkpoints"
            self._driver = AsyncDriver(_LoopTurns(self), dispatcher)
            outcome = await self._driver.run()
            self._end_cause = self._driver.end_cause
            return outcome
        limits = self._spec.limits
        carried = [self._agents[a] for a in resume]
        try:
            while True:
                if carried:
                    active, carried = carried, []
                else:
                    active = self._active(dispatcher)
                    if not active:
                        # What observers asked for on the last response (a stop, a pause, an
                        # injection) takes effect before the run can end.
                        await dispatcher.barrier()
                        await self._hook_point(dispatcher)
                        active = self._active(dispatcher)
                if not active:
                    return self._outcome("finished", None)
                for i, agent in enumerate(active):
                    while True:
                        await self._hook_point(dispatcher)
                        if limits.max_turns is not None and self._turns >= limits.max_turns:
                            return await self._limit("max_turns", limits.max_turns)
                        reached = await self._run_limit()
                        if reached is not None:
                            return reached[0]
                        await self._mark_turn(dispatcher, active[i:])
                        self._turns += 1
                        await self._step(dispatcher, agent)
                        if self._spec.turn_policy != "event_driven" or agent.finished:
                            break
        except Stopped as stop:
            self._end_cause = stop.cause
            return self._outcome("stopped", stop.reason)

    def _active(self, d: HookDispatcher) -> list[AgentRun]:
        """Agents that take a turn this round. Mail due at a finished agent's next turn, or an
        injection queued for it, wakes it; admitting it records the state change."""
        for agent in self._agents.values():
            agent_id = agent.spec.id
            if agent.finished and (
                self._bus.has_mail(agent_id, agent.turn + 1) or d.has_injections(agent_id)
            ):
                agent.finished = False
        return [a for a in self._agents.values() if not a.finished]

    def tool_schemas(self) -> dict[str, ToolSchema]:
        """Every tool this run can offer, as a model request carries it."""
        return {name: tool_schema(tool) for name, tool in self._tools.items()}

    def stop(self, reason: str) -> None:
        """Asks the run to stop, from outside the loop (a cancel). Takes effect at the next hook
        point, like a stop an extension requests."""
        if self._stop_reason is None:
            self._stop_reason = reason
        if self._driver is not None:
            self._poke = asyncio.get_running_loop().create_task(self._driver.poke())

    def _stop_requested(self, dispatcher: HookDispatcher) -> str | None:
        return self._stop_reason or dispatcher.stop_reason

    def _check_stop(self, dispatcher: HookDispatcher) -> None:
        reason = self._stop_requested(dispatcher)
        if reason is not None:
            raise Stopped(reason, self._stop_cause(dispatcher))
        if self._driver is not None and self._driver.ended is not None:
            # Under `async`, another agent ended the run (a gate's `Stop`, a limit): this one
            # stops at its next hook point too.
            raise Stopped(self._driver.ended.reason or "", self._driver.end_cause)

    async def _mark_turn(self, d: HookDispatcher, rest: Sequence[AgentRun]) -> None:
        """Commits the checkpoint a fork can start from, once the observers have caught up and
        whatever they asked for meanwhile (a pause, a stop) has taken effect, so a fork from
        it does what the source did next."""
        await d.barrier()
        await self._hook_point(d)
        checkpoint = checkpoint_of(
            turns=self._turns,
            rest=rest,
            tokens_used=self._tokens_used,
            agents=tuple(self._agents.values()),
            extensions=d.snapshots(),
            mail=self._bus.snapshot(),
            queued=d.queued(),
            spawned=d.spawned_running(),
        )
        await self._writer.commit(Transaction(checkpoint=checkpoint))

    async def _start_fork(self, d: HookDispatcher, fork: ForkStart) -> None:
        started = EventDraft(
            record=LifecycleRecord(
                status="started",
                reason=f"fork of run {fork.source_run_id} after event {fork.fork_seq}",
            ),
            parent_id=self._writer.last_event_id,
        )
        txn, mail = fork_start(fork, self._agents, started)
        self._turns, self._tokens_used = fork.checkpoint.turn, fork.checkpoint.tokens_used
        self._bus.restore(mail)
        d.restore_queued(fork.checkpoint.queued)
        await self._writer.commit(txn)
        self._started_id = started.event_id

    async def _hook_point(self, d: HookDispatcher) -> None:
        """A hook point: a requested pause takes effect, then a requested stop. Under `async`
        one agent pauses the run, and the others wait here."""
        await self._resumed.wait()
        pause = d.take_pause()
        if pause is not None:
            await self._pause(d, pause)
        self._check_stop(d)

    async def _pause(self, d: HookDispatcher, request: PauseRequest) -> None:
        """Waits for the observers first, so the pause's intervention, which an observer may
        still be committing, precedes the `paused` event it parents."""
        self._resumed.clear()
        self._open_pauses += 1
        clock = asyncio.get_running_loop()
        if self._open_pauses == 1:
            self._pause_began = clock.time()
        await d.barrier()
        paused = EventDraft(
            record=LifecycleRecord(status="paused", reason=request.reason),
            parent_id=request.event_id,
        )
        await self._writer.commit(Transaction(events=[paused]))
        ended = await self._pauser.wait(request.reason)
        if ended is not None:
            self.stop(ended)
        else:
            resumed = EventDraft(
                record=LifecycleRecord(status="resumed"), parent_id=paused.event_id
            )
            await self._writer.commit(Transaction(events=[resumed]))
        self._open_pauses -= 1
        if self._open_pauses == 0:
            self._paused_s += clock.time() - self._pause_began
            self._resumed.set()

    def _stop_cause(self, dispatcher: HookDispatcher) -> str | None:
        """An extension's stop intervention; `None` for a stop from outside (a cancel)."""
        return None if self._stop_reason is not None else dispatcher.stop_cause

    async def _limit(
        self,
        limit: Literal["max_turns", "max_tokens", "wall_clock"],
        value: int,
        agent_id: str | None = None,
    ) -> RunOutcome:
        record = LimitRecord(limit=limit, value=value)
        draft = EventDraft(record=record, agent_id=agent_id, parent_id=self._started_id)
        await self._writer.commit(Transaction(events=[draft]))
        self._end_cause = draft.event_id
        return self._outcome("limit", f"{limit} reached ({value})")

    async def _run_limit(self) -> tuple[RunOutcome, str] | None:
        """`max_tokens` or `wall_clock`, recorded once, with the `limit` event. Under `async`
        the agent that sees the limit first records it; the others get the same answer."""
        if self._limit_claim is not None:
            return await self._limit_claim
        limits = self._spec.limits
        reached = (limits.max_tokens is not None and self._tokens_used >= limits.max_tokens) or (
            limits.wall_clock_s is not None and self._elapsed() >= limits.wall_clock_s
        )
        if not reached:
            return None
        self._limit_claim = asyncio.ensure_future(self._record_run_limit())
        return await self._limit_claim

    async def _record_run_limit(self) -> tuple[RunOutcome, str]:
        """Called right after `_run_limit` saw a limit reached, before anything else ran."""
        limits = self._spec.limits
        if limits.max_tokens is not None and self._tokens_used >= limits.max_tokens:
            outcome = await self._limit("max_tokens", limits.max_tokens)
        else:
            assert limits.wall_clock_s is not None, "one of the two was reached"
            outcome = await self._limit("wall_clock", round(limits.wall_clock_s))
        assert self._end_cause is not None, "set by _limit"
        return outcome, self._end_cause

    def _elapsed(self) -> float:
        """Seconds since the run started, paused time left out."""
        return asyncio.get_running_loop().time() - self._clock_start - self._paused_s

    def _outcome(
        self, status: Literal["finished", "stopped", "limit"], reason: str | None
    ) -> RunOutcome:
        return RunOutcome(
            status=status, reason=reason, turns=self._turns, tokens_used=self._tokens_used
        )

    async def _step(self, d: HookDispatcher, agent: AgentRun) -> None:
        agent.turn += 1
        gate = await d.before_turn(agent.info, self._turns, agent.last_input)
        txn = gate.txn
        deliveries = self._bus.take(agent.spec.id, agent.turn)
        txn.events.extend(delivery.draft for delivery in deliveries)
        admitted = [(delivery.message, delivery.draft.event_id) for delivery in deliveries]
        admitted.extend(d.take_injections(agent.spec.id))
        if isinstance(gate.decision, Inject):
            assert gate.intervention_id is not None, "a decision other than Proceed is recorded"
            admitted.extend((m, gate.intervention_id) for m in gate.decision.messages)
        self._admit(txn, agent, admitted)
        txn.events.extend(
            self._bus.post(p.channel, p.sender, p.content, by=p.instance_id, parent_id=p.event_id)
            for p in d.take_posts()
        )
        await self._writer.commit(txn)
        await self._route(d)
        match gate.decision:
            case Stop(reason=reason):
                raise Stopped(f"{gate.decided_by}: {reason}", gate.intervention_id)
            case Skip():
                return
            case _:
                pass
        await self._hook_point(d)

        context = await self._store.context(agent.spec.id)
        assert context is not None, "every agent gets generation 0 in _start"
        messages = context.messages
        compacted = await d.compact_context(agent.info, messages, agent.last_input)
        txn = compacted.txn
        if compacted.value is not None:
            assert compacted.intervention_id is not None, "a compaction is recorded"
            messages = compacted.value
            agent.gen += 1
            agent.length = len(messages)
            agent.last_input = compacted.intervention_id
            txn.new_generations[agent.spec.id] = messages
            txn.agent_states.append(agent.row("ready", self._tokens_used))
        await self._writer.commit(txn)
        await self._hook_point(d)

        spec = agent.spec
        options = RequestOptions(
            tools=spec.tools,
            temperature=spec.temperature,
            top_p=spec.top_p,
            max_output_tokens=spec.max_output_tokens,
            seed=spec.seed,
        )
        requested = await d.before_model_request(agent.info, options, agent.last_input)
        await self._writer.commit(requested.txn)
        await self._hook_point(d)
        request = ModelRequest(
            model=spec.model,
            messages=messages,
            gen=agent.gen,
            tools=tuple(tool_schema(self._tools[name]) for name in requested.value.tools),
            options=requested.value,
        )
        recorded = await self._model.generate(
            AgentCaller(spec.id), request, parent_id=agent.last_input
        )
        self._tokens_used += recorded.response.usage.total
        model_event_id = recorded.event.event_id

        response = await d.after_model_response(
            agent.info, recorded.response.message, model_event_id
        )
        txn = response.txn
        self._admit(txn, agent, [(response.value, response.intervention_id or model_event_id)])
        await self._writer.commit(txn)

        for call in response.value.tool_calls:
            await self._tool_step(d, agent, call, model_event_id, requested.value.tools)
        if not response.value.tool_calls:
            agent.finished = True
            txn = Transaction(agent_states=[agent.row("finished", self._tokens_used)])
            await self._writer.commit(txn)
        await d.observe("after_turn", agent.info, agent.last_input)

    async def _tool_step(
        self,
        d: HookDispatcher,
        agent: AgentRun,
        call: ToolCall,
        model_event_id: str,
        offered: tuple[str, ...],
    ) -> None:
        gate = await d.before_tool_call(agent.info, call, model_event_id)
        txn = gate.txn
        await self._resumed.wait()
        pause = d.take_pause()
        if pause is not None:
            await self._writer.commit(txn)
            txn = Transaction()
            await self._pause(d, pause)
        try:
            self._check_stop(d)
        except Stopped:
            await self._writer.commit(txn)
            raise
        blocked_by: str | None = None
        match gate.decision:
            case Block(result=content, is_error=is_error):
                execution = Execution(
                    ToolResult(call_id=call.id, tool=call.name, content=content, is_error=is_error)
                )
                blocked_by = gate.decided_by
            case decision:
                arguments = (
                    json.dumps(decision.arguments)
                    if isinstance(decision, Rewrite)
                    else call.arguments
                )
                effective = call.model_copy(update={"arguments": arguments})
                execution = await self._runner.execute(
                    d, agent.spec, agent.info, effective, txn, offered, model_event_id
                )

        result = execution.result
        record = ToolCallRecord(
            call=call,
            executed_arguments=execution.executed,
            result=result,
            blocked_by=blocked_by,
            exec_result=execution.exec_result,
            web=execution.web,
        )
        draft = EventDraft(record=record, agent_id=agent.spec.id, parent_id=model_event_id)
        txn.events.append(draft)
        committed = await self._writer.commit(txn)
        tool_event = next(e for e in committed if isinstance(e.record, ToolCallRecord))
        await self._route(d)

        admitted = await d.after_tool_result(agent.info, result, tool_event.event_id)
        txn = admitted.txn
        message = ToolMessage(
            tool_call_id=call.id, content=admitted.value.content, is_error=admitted.value.is_error
        )
        self._admit(txn, agent, [(message, admitted.intervention_id or tool_event.event_id)])
        await self._writer.commit(txn)

    async def _route(self, d: HookDispatcher) -> None:
        """Runs `before_deliver` for every send committed since the last call, per recipient,
        and commits the verdicts. A delay counts the recipient's own turns: held for `n`, a
        message reaches it at the start of its turn `turn + 1 + n`."""
        items = self._bus.unrouted()
        if not items:
            return
        txn = Transaction()
        for item in items:
            recipient = self._agents[item.recipient]
            envelope = Envelope(
                send_event_id=item.send.event_id,
                channel=item.record.channel,
                sender=item.record.sender,
                recipient=item.recipient,
                content=item.record.content,
            )
            outcome = await d.before_deliver(recipient.info, envelope)
            txn.extend(outcome.txn)
            if outcome.content is None:
                txn.deliveries.append(DeliveryChange(item.send.seq, item.recipient, "dropped"))
                continue
            due = recipient.turn + 1 + outcome.delay if outcome.delay else None
            if due is not None or outcome.seconds:
                txn.deliveries.append(
                    DeliveryChange(item.send.seq, item.recipient, "delayed", due_turn=due)
                )
            self._bus.route(
                item,
                outcome.content,
                due_turn=due,
                parent_id=outcome.intervention_id,
                hold_s=outcome.seconds,
            )
        await self._writer.commit(txn)

    def _admit(
        self, txn: Transaction, agent: AgentRun, messages: Sequence[tuple[ChatMessage, str]]
    ) -> None:
        """Each message comes with the event its content is from."""
        if not messages:
            return
        txn.messages.extend((agent.spec.id, m) for m, _ in messages)
        agent.length += len(messages)
        agent.last_input = messages[-1][1]
        txn.agent_states.append(agent.row("ready", self._tokens_used))


class _LoopTurns:
    """The loop as the `async` driver sees it (`swarmeval.runtime.concurrency.Turns`)."""

    def __init__(self, loop: RunLoop) -> None:
        self._loop = loop

    @property
    def agents(self) -> Sequence[AgentRun]:
        return tuple(self._loop._agents.values())  # pyright: ignore[reportPrivateUsage]

    @property
    def bus(self) -> MessageBus:
        return self._loop._bus  # pyright: ignore[reportPrivateUsage]

    @property
    def max_turns(self) -> int | None:
        return self._loop._spec.limits.max_turns  # pyright: ignore[reportPrivateUsage]

    async def hook_point(self, d: HookDispatcher) -> None:
        await self._loop._hook_point(d)  # pyright: ignore[reportPrivateUsage]

    def stop_requested(self, d: HookDispatcher) -> bool:
        return self._loop._stop_requested(d) is not None  # pyright: ignore[reportPrivateUsage]

    def time_left(self) -> float | None:
        limit = self._loop._spec.limits.wall_clock_s  # pyright: ignore[reportPrivateUsage]
        return None if limit is None else limit - self._loop._elapsed()  # pyright: ignore[reportPrivateUsage]

    async def run_limit(self) -> tuple[RunOutcome, str] | None:
        return await self._loop._run_limit()  # pyright: ignore[reportPrivateUsage]

    async def agent_limit(self, agent: AgentRun) -> str:
        limit = self._loop._spec.limits.max_turns  # pyright: ignore[reportPrivateUsage]
        assert limit is not None, "the driver checks max_turns first"
        await self._loop._limit("max_turns", limit, agent.spec.id)  # pyright: ignore[reportPrivateUsage]
        cause = self._loop._end_cause  # pyright: ignore[reportPrivateUsage]
        assert cause is not None, "set by _limit"
        return cause

    async def step(self, d: HookDispatcher, agent: AgentRun) -> None:
        self._loop._turns += 1  # pyright: ignore[reportPrivateUsage]
        await self._loop._step(d, agent)  # pyright: ignore[reportPrivateUsage]

    def outcome(
        self, status: Literal["finished", "stopped", "limit"], reason: str | None
    ) -> RunOutcome:
        return self._loop._outcome(status, reason)  # pyright: ignore[reportPrivateUsage]
