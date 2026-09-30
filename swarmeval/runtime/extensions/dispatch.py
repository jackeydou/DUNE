"""Running hooks: ordering, chaining, gating, recording interventions, observers and barriers.

Every public hook method first waits for the observer barrier: all events committed so far have
been seen by every `on_event` handler, and any action they requested is in effect. Effects a hook
produces (emits, alerts, interventions, state changes) come back as a `Transaction` for the caller
to commit with the step it belongs to; observer effects are committed here.
"""

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, JsonValue
from pydantic_core import to_json

from swarmeval.runtime.extensions.api import (
    AgentInfo,
    Allow,
    AnyHandler,
    Block,
    ExtensionError,
    HookContext,
    Inject,
    Proceed,
    Rewrite,
    RunInfo,
    Skip,
    StateCell,
    Stop,
    ToolDecision,
    TurnDecision,
    TurnInfo,
)
from swarmeval.runtime.extensions.registry import LoadedExtension
from swarmeval.runtime.messages import (
    AssistantMessage,
    ChatMessage,
    RequestOptions,
    ToolCall,
    UserMessage,
)
from swarmeval.runtime.ports import ModelClient, SandboxExecutor
from swarmeval.runtime.records import (
    CommittedEvent,
    EventDraft,
    HookName,
    InterventionRecord,
    ToolResult,
    Transaction,
)
from swarmeval.runtime.tools import WorkerTool
from swarmeval.runtime.writer import RunWriter

type CallSite = HookName | Literal["tool"]


@dataclass
class GateOutcome[D]:
    decision: D
    decided_by: str | None
    txn: Transaction


@dataclass
class TransformOutcome[T]:
    value: T
    txn: Transaction


class _Instance:
    def __init__(self, loaded: LoadedExtension, state: BaseModel, seed: int, timeout_s: float):
        self.loaded = loaded
        self.id = loaded.instance_id
        self.state = StateCell(state)
        self.committed_state = state.model_dump_json()
        digest = hashlib.sha256(f"{seed}:{self.id}".encode()).digest()
        self.rng = random.Random(int.from_bytes(digest[:8], "big"))
        self.timeout_s = loaded.extension.hook_timeout_s or timeout_s

    def handlers(self, hook: HookName) -> list[AnyHandler]:
        return self.loaded.registrations.handlers.get(hook, [])


def sha256_json(value: JsonValue) -> str:
    return hashlib.sha256(to_json(value)).hexdigest()


def _dump(value: BaseModel | Sequence[BaseModel]) -> JsonValue:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return [item.model_dump(mode="json") for item in value]


class HookDispatcher:
    def __init__(
        self,
        *,
        extensions: Sequence[LoadedExtension],
        run: RunInfo,
        agents: Mapping[str, AgentInfo],
        seed: int,
        states: Mapping[str, JsonValue],
        model_client: ModelClient,
        sandbox_executor: SandboxExecutor,
        writer: RunWriter,
        default_timeout_s: float,
    ) -> None:
        self._run = run
        self._agents = dict(agents)
        self._model_client = model_client
        self._sandbox_executor = sandbox_executor
        self._writer = writer
        self._instances: list[_Instance] = []
        for loaded in extensions:
            saved = states.get(loaded.instance_id)
            state = (
                loaded.initial_state()
                if saved is None
                else loaded.extension.state.model_validate(saved)
            )
            self._instances.append(_Instance(loaded, state, seed, default_timeout_s))

        self.stop_reason: str | None = None
        self._injections: dict[str, list[UserMessage]] = {}
        self._spawned: dict[asyncio.Task[None], _Instance] = {}
        self._failure: ExtensionError | None = None
        self._queue: asyncio.Queue[CommittedEvent] = asyncio.Queue()
        self._published = 0
        self._processed = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._observer: asyncio.Task[None] | None = None
        self._sandbox_calls: dict[str, int] = {}
        writer.subscribe(self._on_commit)

    # ContextHost

    @property
    def model_client(self) -> ModelClient:
        return self._model_client

    @property
    def sandbox_executor(self) -> SandboxExecutor:
        return self._sandbox_executor

    @property
    def writer(self) -> RunWriter:
        return self._writer

    def agent_ids(self) -> tuple[str, ...]:
        return self._run.agent_ids

    def sandbox_call_id(self, instance_id: str) -> str:
        n = self._sandbox_calls.get(instance_id, 0) + 1
        self._sandbox_calls[instance_id] = n
        return f"ext:{instance_id}:{n}"

    def request_stop(self, instance_id: str, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = f"{instance_id}: {reason}"

    def queue_inject(self, instance_id: str, agent_id: str, message: UserMessage) -> None:
        self._injections.setdefault(agent_id, []).append(message)

    def spawn(self, ctx: HookContext[Any], coro: Coroutine[Any, Any, None]) -> None:
        instance = self._instance(ctx.instance_id)

        async def run() -> None:
            await coro
            await self._writer.commit(self._drain(instance, ctx))

        task = asyncio.create_task(run())
        self._spawned[task] = instance
        task.add_done_callback(self._spawn_done)

    # Lifecycle

    def start(self) -> None:
        self._observer = asyncio.create_task(self._observe_events())

    async def close(self) -> None:
        self._writer.unsubscribe(self._on_commit)
        for task in [*self._spawned, *([self._observer] if self._observer else [])]:
            task.cancel()
        await asyncio.gather(*self._spawned, return_exceptions=True)
        if self._observer:
            await asyncio.gather(self._observer, return_exceptions=True)

    async def barrier(self) -> None:
        while self._processed < self._published and self._failure is None:
            await self._idle.wait()
        if self._failure is not None:
            raise self._failure

    async def settle(self) -> None:
        """Waits until observers have seen every committed event and every spawned task has
        finished and committed its effects, including work those spawn in turn. Each task gets its
        instance's hook timeout, counted from this call; one still running after that fails the
        run."""
        loop = asyncio.get_running_loop()
        deadlines: dict[asyncio.Task[None], float] = {}
        while True:
            for task in [t for t in self._spawned if t.done()]:
                self._spawn_done(task)
            await self.barrier()
            running = [t for t in self._spawned if not t.done()]
            if not self._spawned:
                return
            if not running:
                continue
            now = loop.time()
            for task in running:
                deadlines.setdefault(task, now + self._spawned[task].timeout_s)
            first = min(running, key=deadlines.__getitem__)
            if now >= deadlines[first]:
                instance = self._spawned[first]
                raise ExtensionError(
                    instance.id,
                    "spawn",
                    f"spawned task still running {instance.timeout_s}s after the run ended. "
                    "Bound its work, or raise the extension's `hook_timeout_s`.",
                )
            await asyncio.wait(
                running, timeout=deadlines[first] - now, return_when=asyncio.FIRST_COMPLETED
            )

    def fail(self, err: ExtensionError) -> None:
        """The run has failed: observers get no further events, and `close` cancels spawned
        work."""
        if self._failure is None:
            self._failure = err

    def take_injections(self, agent_id: str) -> list[UserMessage]:
        return self._injections.pop(agent_id, [])

    # Hooks

    async def observe(
        self, hook: Literal["on_run_start", "after_turn", "on_run_end"], agent: AgentInfo | None
    ) -> None:
        await self.barrier()
        txn = Transaction()
        for instance, handler in self._handlers(hook):
            _, effects = await self._call(instance, hook, agent, handler)
            txn.extend(effects)
        await self._writer.commit(txn)

    async def before_turn(self, agent: AgentInfo, turn: int) -> GateOutcome[TurnDecision]:
        await self.barrier()
        return await self._gate(
            "before_turn",
            agent,
            TurnInfo(turn=turn),
            None,
            Proceed(),
            (Proceed, Skip, Inject, Stop),
        )

    async def compact_context(
        self, agent: AgentInfo, messages: tuple[ChatMessage, ...]
    ) -> TransformOutcome[tuple[ChatMessage, ...] | None]:
        await self.barrier()
        txn = Transaction()
        current = messages
        for instance, handler in self._handlers("compact_context"):
            result, effects = await self._call(instance, "compact_context", agent, handler, current)
            txn.extend(effects)
            if result is None:
                continue
            if not isinstance(result, tuple):
                raise ExtensionError(
                    instance.id,
                    "compact_context",
                    f"returned {type(result).__name__}; return a tuple of messages or None.",
                )
            new = cast(tuple[ChatMessage, ...], result)
            if not new:
                raise ExtensionError(
                    instance.id,
                    "compact_context",
                    "returned an empty context. A generation needs at least one message; "
                    "return None to keep the current context.",
                )
            if new != current:
                txn.events.append(
                    self._intervention(instance, "compact_context", "compact", None, current, new)
                )
                current = new
        return TransformOutcome(None if current is messages else current, txn)

    async def before_model_request(
        self, agent: AgentInfo, options: RequestOptions
    ) -> TransformOutcome[RequestOptions]:
        await self.barrier()
        outcome = await self._transform(
            "before_model_request", agent, options, None, RequestOptions
        )
        extra = set(outcome.value.tools) - set(options.tools)
        if extra:
            raise ExtensionError(
                self._last_changer("before_model_request", outcome.txn),
                "before_model_request",
                f"offered tools the agent does not have: {', '.join(sorted(extra))}. "
                "This hook may only narrow the tool list.",
            )
        return outcome

    async def after_model_response(
        self, agent: AgentInfo, message: AssistantMessage, target_event_id: str
    ) -> TransformOutcome[AssistantMessage]:
        await self.barrier()
        return await self._transform(
            "after_model_response", agent, message, target_event_id, AssistantMessage
        )

    async def before_tool_call(
        self, agent: AgentInfo, call: ToolCall, target_event_id: str
    ) -> GateOutcome[ToolDecision]:
        await self.barrier()
        return await self._gate(
            "before_tool_call", agent, call, target_event_id, Allow(), (Allow, Rewrite, Block)
        )

    async def after_tool_result(
        self, agent: AgentInfo, result: ToolResult, target_event_id: str
    ) -> TransformOutcome[ToolResult]:
        await self.barrier()
        return await self._transform(
            "after_tool_result", agent, result, target_event_id, ToolResult
        )

    async def run_worker_tool(
        self, tool: WorkerTool, agent: AgentInfo, args: BaseModel
    ) -> tuple[str, Transaction]:
        result, txn = await self._call(self._instance(tool.owner), "tool", agent, tool.run, args)
        if not isinstance(result, str):
            raise ExtensionError(
                tool.owner, "tool", f"tool `{tool.name}` returned {type(result).__name__}, not str."
            )
        return result, txn

    # Internals

    def _instance(self, instance_id: str) -> _Instance:
        return next(i for i in self._instances if i.id == instance_id)

    def _handlers(self, hook: HookName) -> list[tuple[_Instance, AnyHandler]]:
        return [(i, h) for i in self._instances for h in i.handlers(hook)]

    def _context(
        self, instance: _Instance, site: CallSite, agent: AgentInfo | None
    ) -> HookContext[Any]:
        return HookContext(
            host=self,
            instance_id=instance.id,
            hook=site,
            run=self._run,
            agent=agent,
            state=instance.state,
            rng=instance.rng,
        )

    async def _call(
        self,
        instance: _Instance,
        site: CallSite,
        agent: AgentInfo | None,
        handler: Callable[..., Awaitable[Any]],
        *payload: Any,
    ) -> tuple[Any, Transaction]:
        ctx = self._context(instance, site, agent)
        try:
            async with asyncio.timeout(instance.timeout_s):
                result = await handler(ctx, *payload)
        except TimeoutError as err:
            raise ExtensionError(
                instance.id, site, f"timed out after {instance.timeout_s}s"
            ) from err
        except ExtensionError:
            raise
        except Exception as err:
            raise ExtensionError(instance.id, site, f"{type(err).__name__}: {err}") from err
        return result, self._drain(instance, ctx)

    def _drain(self, instance: _Instance, ctx: HookContext[Any]) -> Transaction:
        txn, ctx.pending = ctx.pending, Transaction()
        state = instance.state.value.model_dump_json()
        if state != instance.committed_state:
            txn.extension_states[instance.id] = instance.state.value.model_dump(mode="json")
            instance.committed_state = state
        return txn

    async def _transform[T: BaseModel](
        self,
        hook: HookName,
        agent: AgentInfo,
        value: T,
        target_event_id: str | None,
        expected: type[T],
    ) -> TransformOutcome[T]:
        txn = Transaction()
        for instance, handler in self._handlers(hook):
            result, effects = await self._call(instance, hook, agent, handler, value)
            txn.extend(effects)
            if not isinstance(result, expected):
                raise ExtensionError(
                    instance.id,
                    hook,
                    f"returned {type(result).__name__}; return a {expected.__name__}, "
                    "the input unchanged if there is nothing to change.",
                )
            if result != value:
                txn.events.append(
                    self._intervention(instance, hook, "rewrite", target_event_id, value, result)
                )
                value = result
        return TransformOutcome(value, txn)

    async def _gate[D: BaseModel](
        self,
        hook: HookName,
        agent: AgentInfo,
        payload: BaseModel,
        target_event_id: str | None,
        default: D,
        allowed: tuple[type[BaseModel], ...],
    ) -> GateOutcome[D]:
        txn = Transaction()
        for instance, handler in self._handlers(hook):
            decision, effects = await self._call(instance, hook, agent, handler, payload)
            txn.extend(effects)
            if not isinstance(decision, allowed):
                raise ExtensionError(
                    instance.id,
                    hook,
                    f"returned {type(decision).__name__}; return one of "
                    f"{', '.join(t.__name__ for t in allowed)}.",
                )
            if type(decision) is not type(default):
                action = str(decision.model_dump()["kind"])
                txn.events.append(
                    self._intervention(instance, hook, action, target_event_id, payload, decision)
                )
                return GateOutcome(cast(D, decision), instance.id, txn)
        return GateOutcome(default, None, txn)

    def _intervention(
        self,
        instance: _Instance,
        hook: HookName,
        action: str,
        target_event_id: str | None,
        before: BaseModel | Sequence[BaseModel],
        after: BaseModel | Sequence[BaseModel],
    ) -> EventDraft:
        record = InterventionRecord(
            hook=hook,
            action=action,
            target_event_id=target_event_id,
            before_sha256=sha256_json(_dump(before)),
            after=_dump(after),
        )
        return EventDraft(record=record, extension=instance.id, parent_id=target_event_id)

    def _last_changer(self, hook: HookName, txn: Transaction) -> str:
        changers = [
            e.extension
            for e in txn.events
            if isinstance(e.record, InterventionRecord) and e.record.hook == hook and e.extension
        ]
        return changers[-1]

    def _on_commit(self, events: Sequence[CommittedEvent]) -> None:
        for event in events:
            self._published += 1
            self._idle.clear()
            self._queue.put_nowait(event)

    async def _observe_events(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if self._failure is None:
                    agent = self._agents.get(event.agent_id) if event.agent_id else None
                    for instance, handler in self._handlers("on_event"):
                        if event.extension == instance.id:
                            continue
                        _, txn = await self._call(instance, "on_event", agent, handler, event)
                        await self._writer.commit(txn)
            except ExtensionError as err:
                self._failure = err
            finally:
                self._processed += 1
                if self._processed >= self._published:
                    self._idle.set()

    def _spawn_done(self, task: asyncio.Task[None]) -> None:
        """Done callback, also called by `settle`; the first call wins."""
        instance = self._spawned.pop(task, None)
        if instance is None or task.cancelled() or self._failure is not None:
            return
        exc = task.exception()
        if exc is not None:
            failure = ExtensionError(instance.id, "spawn", f"{type(exc).__name__}: {exc}")
            failure.__cause__ = exc
            self._failure = failure
            self._idle.set()
