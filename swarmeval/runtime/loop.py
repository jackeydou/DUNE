"""The agent loop: one run, its agents taking turns, every step recorded before it is admitted.

A step is one model call plus every tool call it makes. Content reaches an agent's context in
two commits: first as observed (record), then as the agent will see it after hooks (admit).
See docs/agent-runtime.md.
"""

import json
from collections.abc import Sequence
from typing import Literal

from swarmeval.gateway.bus import MessageBus
from swarmeval.runtime.execute import Execution, ToolRunner
from swarmeval.runtime.extensions.api import (
    AgentInfo,
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
from swarmeval.runtime.extensions.interventions import sha256_json
from swarmeval.runtime.extensions.registry import LoadedExtension
from swarmeval.runtime.fork import (
    ForkStart,
    ReplaceDelivery,
    check_edits,
    delivery_intervention,
    edit_intervention,
    edited_contexts,
    replace_mail,
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
    AgentCheckpoint,
    AgentStateRow,
    Checkpoint,
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
    AgentSpec,
    RunConfigError,
    RunOutcome,
    RunSpec,
    initial_context,
)
from swarmeval.runtime.tools import (
    SandboxTool,
    Tool,
    WebTool,
    tool_schema,
)
from swarmeval.runtime.writer import RunWriter


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
        self.last_input = ""
        """The last event whose content was admitted into this agent's context: the parent of
        its next model call (docs/event-log.md#causal-parents). Set when generation 0 commits."""


class _Stopped(Exception):
    def __init__(self, reason: str, cause: str | None) -> None:
        self.reason = reason
        self.cause = cause
        """The event that stopped the run, if an event did."""


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
        writer.subscribe(self._bus.on_commit)
        bus_tool = self._bus.tool()
        self._tools: dict[str, Tool] = {t.name: t for t in tools}
        self._tools[bus_tool.name] = bus_tool
        for ext in extensions:
            self._tools.update({t.name: t for t in ext.registrations.tools})
        self._agents = {a.id: _AgentRun(a) for a in spec.agents}
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
        dispatcher.start()
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
            txn.agent_states.append(self._state_row(agent, "ready"))
        await self._writer.commit(txn)
        self._started_id = started.event_id

    async def _drive(self, dispatcher: HookDispatcher, resume: Sequence[str] = ()) -> RunOutcome:
        """`resume` is a fork's round in progress: the agents left in it."""
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
                    await self._hook_point(dispatcher)
                    if limits.max_turns is not None and self._turns >= limits.max_turns:
                        return await self._limit("max_turns", limits.max_turns)
                    if limits.max_tokens is not None and self._tokens_used >= limits.max_tokens:
                        return await self._limit("max_tokens", limits.max_tokens)
                    await self._mark_turn(dispatcher, active[i:])
                    self._turns += 1
                    await self._step(dispatcher, agent)
        except _Stopped as stop:
            self._end_cause = stop.cause
            return self._outcome("stopped", stop.reason)

    def _active(self, d: HookDispatcher) -> list[_AgentRun]:
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

    def _stop_requested(self, dispatcher: HookDispatcher) -> str | None:
        return self._stop_reason or dispatcher.stop_reason

    def _check_stop(self, dispatcher: HookDispatcher) -> None:
        reason = self._stop_requested(dispatcher)
        if reason is not None:
            raise _Stopped(reason, self._stop_cause(dispatcher))

    async def _mark_turn(self, d: HookDispatcher, rest: Sequence["_AgentRun"]) -> None:
        """Commits the checkpoint a fork can start from, once the observers have caught up."""
        await d.barrier()
        checkpoint = Checkpoint(
            turn=self._turns,
            round=tuple(a.spec.id for a in rest),
            tokens_used=self._tokens_used,
            agents={
                a.spec.id: AgentCheckpoint(
                    gen=a.gen,
                    length=a.length,
                    turn=a.turn,
                    finished=a.finished,
                    last_input=a.last_input,
                )
                for a in self._agents.values()
            },
            extensions=d.snapshots(),
            mail=self._bus.snapshot(),
            queued=d.queued(),
            spawned=d.spawned_running(),
        )
        await self._writer.commit(Transaction(checkpoint=checkpoint))

    async def _start_fork(self, d: HookDispatcher, fork: ForkStart) -> None:
        """The source's state at the checkpoint, then the edits, each an intervention. Its
        contexts are copied at their generation numbers, so the source's events still explain
        them."""
        checkpoint = fork.checkpoint
        started = EventDraft(
            record=LifecycleRecord(
                status="started",
                reason=f"fork of run {fork.source_run_id} after event {fork.fork_seq}",
            ),
            parent_id=self._writer.last_event_id,
        )
        txn = Transaction(events=[started], inherited_mail=list(checkpoint.mail))
        txn.extension_states.update(
            {k: ExtensionSnapshot(v.state, v.rng_uses) for k, v in checkpoint.extensions.items()}
        )
        self._turns, self._tokens_used = checkpoint.turn, checkpoint.tokens_used
        for agent in self._agents.values():
            saved = checkpoint.agents[agent.spec.id]
            agent.gen, agent.length, agent.turn = saved.gen, saved.length, saved.turn
            agent.finished, agent.last_input = saved.finished, saved.last_input
            txn.inherited[(agent.spec.id, saved.gen)] = fork.contexts[agent.spec.id]
            txn.agent_states.append(
                self._state_row(agent, "finished" if saved.finished else "ready")
            )
        check_edits(fork.edits, fork.contexts, checkpoint.mail)
        for agent_id, messages in edited_contexts(fork.contexts, fork.edits).items():
            agent = self._agents[agent_id]
            before = sha256_json([m.model_dump(mode="json") for m in fork.contexts[agent_id]])
            draft = edit_intervention(agent_id, before, messages, started.event_id)
            txn.events.append(draft)
            agent.gen, agent.length, agent.last_input = agent.gen + 1, len(messages), draft.event_id
            txn.new_generations[agent_id] = messages
            txn.agent_states.append(self._state_row(agent, "ready"))
        mail = list(checkpoint.mail)
        for edit in fork.edits:
            if isinstance(edit, ReplaceDelivery):
                i = next(i for i, m in enumerate(mail) if replace_mail(m, edit))
                draft = delivery_intervention(
                    mail[i], edit, sha256_json(mail[i].content), started.event_id
                )
                txn.events.append(draft)
                mail[i] = mail[i].model_copy(
                    update={"content": edit.content, "parent_id": draft.event_id}
                )
        self._bus.restore(mail)
        d.restore_queued(checkpoint.queued)
        await self._writer.commit(txn)
        self._started_id = started.event_id

    async def _hook_point(self, d: HookDispatcher) -> None:
        """A hook point: a requested pause takes effect, then a requested stop."""
        pause = d.take_pause()
        if pause is not None:
            await self._pause(d, pause)
        self._check_stop(d)

    async def _pause(self, d: HookDispatcher, request: PauseRequest) -> None:
        """Waits for the observers first, so the pause's intervention, which an observer may
        still be committing, precedes the `paused` event it parents."""
        await d.barrier()
        paused = EventDraft(
            record=LifecycleRecord(status="paused", reason=request.reason),
            parent_id=request.event_id,
        )
        await self._writer.commit(Transaction(events=[paused]))
        ended = await self._pauser.wait(request.reason)
        if ended is not None:
            self.stop(ended)
            return
        resumed = EventDraft(record=LifecycleRecord(status="resumed"), parent_id=paused.event_id)
        await self._writer.commit(Transaction(events=[resumed]))

    def _stop_cause(self, dispatcher: HookDispatcher) -> str | None:
        """An extension's stop intervention; `None` for a stop from outside (a cancel)."""
        return None if self._stop_reason is not None else dispatcher.stop_cause

    async def _limit(self, limit: Literal["max_turns", "max_tokens"], value: int) -> RunOutcome:
        draft = EventDraft(record=LimitRecord(limit=limit, value=value), parent_id=self._started_id)
        await self._writer.commit(Transaction(events=[draft]))
        self._end_cause = draft.event_id
        return self._outcome("limit", f"{limit} reached ({value})")

    def _outcome(
        self, status: Literal["finished", "stopped", "limit"], reason: str | None
    ) -> RunOutcome:
        return RunOutcome(
            status=status, reason=reason, turns=self._turns, tokens_used=self._tokens_used
        )

    async def _step(self, d: HookDispatcher, agent: _AgentRun) -> None:
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
                raise _Stopped(f"{gate.decided_by}: {reason}", gate.intervention_id)
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
            txn.agent_states.append(self._state_row(agent, "ready"))
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
            txn = Transaction(agent_states=[self._state_row(agent, "finished")])
            await self._writer.commit(txn)
        await d.observe("after_turn", agent.info, agent.last_input)

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
        pause = d.take_pause()
        if pause is not None:
            await self._writer.commit(txn)
            txn = Transaction()
            await self._pause(d, pause)
        reason = self._stop_requested(d)
        if reason is not None:
            await self._writer.commit(txn)
            raise _Stopped(reason, self._stop_cause(d))
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
            if due is not None:
                txn.deliveries.append(
                    DeliveryChange(item.send.seq, item.recipient, "delayed", due_turn=due)
                )
            self._bus.route(item, outcome.content, due_turn=due, parent_id=outcome.intervention_id)
        await self._writer.commit(txn)

    def _admit(
        self, txn: Transaction, agent: _AgentRun, messages: Sequence[tuple[ChatMessage, str]]
    ) -> None:
        """Each message comes with the event its content is from."""
        if not messages:
            return
        txn.messages.extend((agent.spec.id, m) for m, _ in messages)
        agent.length += len(messages)
        agent.last_input = messages[-1][1]
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
