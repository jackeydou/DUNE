"""The public extension API: declaring an extension, registering hooks and tools, and the
context a hook runs with. This module is a contract with extension authors; see
`API_VERSION` and docs/agent-runtime.md before changing anything here."""

import random
import re
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, get_args, overload

from pydantic import BaseModel, JsonValue

from swarmeval.runtime.messages import (
    AssistantMessage,
    ChatMessage,
    Frozen,
    ModelRequest,
    ModelResponse,
    RequestOptions,
    ToolCall,
    UserMessage,
)
from swarmeval.runtime.ports import ExtensionCaller, ModelClient, SandboxExecutor
from swarmeval.runtime.records import (
    AlertRecord,
    CommittedEvent,
    EventDraft,
    Exec,
    ExecResult,
    ExtensionEmitRecord,
    HookName,
    InterventionRecord,
    Record,
    SandboxExecRecord,
    ToolResult,
    Transaction,
)
from swarmeval.runtime.tools import SandboxTool, Tool, WorkerTool
from swarmeval.runtime.writer import RunWriter

API_VERSION = 1
SUPPORTED_API_VERSIONS = frozenset({1})
HOOK_NAMES: frozenset[str] = frozenset(get_args(HookName))
_ID_PATTERN = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")


class ExtensionDefinitionError(Exception):
    """An extension used the API wrongly while being declared or set up."""


class ExtensionError(Exception):
    """A hook raised or timed out. The run fails; see the chained exception for the cause."""

    def __init__(
        self, instance_id: str, hook: HookName | Literal["tool", "spawn"], detail: str
    ) -> None:
        super().__init__(f"extension `{instance_id}` failed in `{hook}`: {detail}")
        self.instance_id = instance_id
        self.hook: HookName | Literal["tool", "spawn"] = hook


class NoConfig(BaseModel):
    pass


class NoState(BaseModel):
    pass


# Decisions a gate hook returns.


class Proceed(Frozen):
    kind: Literal["proceed"] = "proceed"


class Skip(Frozen):
    kind: Literal["skip"] = "skip"
    reason: str


class Inject(Frozen):
    kind: Literal["inject"] = "inject"
    messages: tuple[UserMessage, ...]


class Stop(Frozen):
    kind: Literal["stop"] = "stop"
    reason: str


type TurnDecision = Proceed | Skip | Inject | Stop


class Allow(Frozen):
    kind: Literal["allow"] = "allow"


class Rewrite(Frozen):
    kind: Literal["rewrite"] = "rewrite"
    arguments: dict[str, JsonValue]


class Block(Frozen):
    kind: Literal["block"] = "block"
    result: str
    is_error: bool = True


type ToolDecision = Allow | Rewrite | Block


class TurnInfo(Frozen):
    turn: int
    """Run-wide turn number, starting at 1."""


class RunInfo(Frozen):
    run_id: str
    agent_ids: tuple[str, ...]


class AgentInfo(Frozen):
    id: str
    model: str
    sandbox_id: str | None
    tools: tuple[str, ...]


# What a hook runs with.


class ContextHost(Protocol):
    """Implemented by the dispatcher. Hooks never see it directly."""

    @property
    def model_client(self) -> ModelClient: ...
    @property
    def sandbox_executor(self) -> SandboxExecutor: ...
    @property
    def writer(self) -> RunWriter: ...
    def agent_ids(self) -> tuple[str, ...]: ...
    def request_stop(self, instance_id: str, reason: str) -> None: ...
    def queue_inject(self, instance_id: str, agent_id: str, message: UserMessage) -> None: ...
    def spawn(self, ctx: "HookContext[Any]", coro: Coroutine[Any, Any, None]) -> None: ...


@dataclass
class StateCell[S: BaseModel]:
    value: S


class ExtensionModel:
    """Model calls go through model-gateway under this instance's key and are recorded."""

    def __init__(self, host: ContextHost, instance_id: str) -> None:
        self._host = host
        self._instance_id = instance_id

    async def generate(self, request: ModelRequest) -> ModelResponse:
        caller = ExtensionCaller(self._instance_id)
        recorded = await self._host.model_client.generate(caller, request)
        return recorded.response


class ExtensionSandbox:
    """Commands run through sandboxd; their record is attributed to this instance."""

    def __init__(self, host: ContextHost, instance_id: str) -> None:
        self._host = host
        self._instance_id = instance_id

    async def exec(self, sandbox_id: str, command: Exec, os_user: str | None = None) -> ExecResult:
        result = await self._host.sandbox_executor.exec(sandbox_id, os_user, command)
        record = SandboxExecRecord(sandbox_id=sandbox_id, command=command, result=result)
        draft = EventDraft(record=record, extension=self._instance_id)
        await self._host.writer.commit(Transaction(events=[draft]))
        return result


class Actions:
    """Requests that take effect at the loop's next hook point."""

    def __init__(self, ctx: "HookContext[Any]") -> None:
        self._ctx = ctx

    def alert(
        self,
        message: str,
        *,
        severity: Literal["low", "medium", "high"],
        event_ids: tuple[str, ...] | list[str] = (),
    ) -> None:
        record = AlertRecord(message=message, severity=severity, event_ids=tuple(event_ids))
        self._ctx.pending.events.append(self._ctx.draft(record))

    def stop(self, reason: str) -> None:
        """Stops the run at the next hook point. Running tool calls finish first."""
        self._ctx.pending.events.append(self._ctx.intervention("stop", {"reason": reason}))
        self._ctx.host.request_stop(self._ctx.instance_id, reason)

    def inject(self, agent_id: str, content: str) -> None:
        """Queues a user message for `agent_id`, admitted at its next `before_turn`."""
        if agent_id not in self._ctx.host.agent_ids():
            raise ExtensionDefinitionError(
                f"extension `{self._ctx.instance_id}` injected into unknown agent `{agent_id}`. "
                f"Agents in this run: {', '.join(self._ctx.host.agent_ids())}."
            )
        record = self._ctx.intervention("inject", {"agent_id": agent_id, "content": content})
        self._ctx.pending.events.append(record)
        self._ctx.host.queue_inject(self._ctx.instance_id, agent_id, UserMessage(content=content))


class HookContext[S: BaseModel]:
    """What a hook or worker tool receives. One is built per call."""

    def __init__(
        self,
        *,
        host: ContextHost,
        instance_id: str,
        hook: HookName | Literal["tool"],
        run: RunInfo,
        agent: AgentInfo | None,
        state: StateCell[S],
        rng: random.Random,
    ) -> None:
        self.host = host
        self.instance_id = instance_id
        self.hook: HookName | Literal["tool"] = hook
        self.run = run
        self.agent = agent
        self.rng = rng
        self.model = ExtensionModel(host, instance_id)
        self.sandbox = ExtensionSandbox(host, instance_id)
        self.actions = Actions(self)
        self.pending = Transaction()
        self._state = state

    @property
    def state(self) -> S:
        return self._state.value

    @state.setter
    def state(self, value: S) -> None:
        self._state.value = value

    def emit(self, name: str, data: BaseModel | JsonValue) -> None:
        payload: JsonValue = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
        self.pending.events.append(self.draft(ExtensionEmitRecord(name=name, data=payload)))

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Runs `coro` in the background. It may outlive this hook call; an exception in it fails
        the run at the next hook point."""
        self.host.spawn(self, coro)

    def draft(self, record: Record) -> EventDraft:
        return EventDraft(
            record=record,
            agent_id=self.agent.id if self.agent else None,
            extension=self.instance_id,
        )

    def intervention(self, action: str, after: JsonValue) -> EventDraft:
        record = InterventionRecord(
            hook=self.hook, action=action, target_event_id=None, before_sha256=None, after=after
        )
        return self.draft(record)


# Hook handler signatures, one per hook name.

type ObserveHandler[S: BaseModel] = Callable[[HookContext[S]], Awaitable[None]]
type BeforeTurnHandler[S: BaseModel] = Callable[[HookContext[S], TurnInfo], Awaitable[TurnDecision]]
type CompactHandler[S: BaseModel] = Callable[
    [HookContext[S], tuple[ChatMessage, ...]], Awaitable[tuple[ChatMessage, ...] | None]
]
type RequestHandler[S: BaseModel] = Callable[
    [HookContext[S], RequestOptions], Awaitable[RequestOptions]
]
type ResponseHandler[S: BaseModel] = Callable[
    [HookContext[S], AssistantMessage], Awaitable[AssistantMessage]
]
type ToolCallHandler[S: BaseModel] = Callable[[HookContext[S], ToolCall], Awaitable[ToolDecision]]
type ToolResultHandler[S: BaseModel] = Callable[[HookContext[S], ToolResult], Awaitable[ToolResult]]
type EventHandler[S: BaseModel] = Callable[[HookContext[S], CommittedEvent], Awaitable[None]]
type WorkerToolFn[S: BaseModel, A: BaseModel] = Callable[[HookContext[S], A], Awaitable[str]]
type AnyHandler = Callable[..., Awaitable[Any]]


@dataclass
class Registrations:
    handlers: dict[str, list[AnyHandler]] = field(default_factory=dict[str, list[AnyHandler]])
    tools: list[Tool] = field(default_factory=list[Tool])


class ExtensionAPI[C: BaseModel, S: BaseModel]:
    """Handed to an extension's setup function. Registration closes when setup returns."""

    def __init__(self, *, instance_id: str, config: C) -> None:
        self.instance_id = instance_id
        self.config = config
        self.registrations = Registrations()
        self._open = True

    def close(self) -> None:
        self._open = False

    def _check_open(self, what: str) -> None:
        if not self._open:
            raise ExtensionDefinitionError(
                f"extension `{self.instance_id}` called {what} after setup returned. "
                "Register every hook and tool inside setup."
            )

    @overload
    def on(
        self, hook: Literal["on_run_start", "after_turn", "on_run_end"]
    ) -> Callable[[ObserveHandler[S]], ObserveHandler[S]]: ...
    @overload
    def on(
        self, hook: Literal["before_turn"]
    ) -> Callable[[BeforeTurnHandler[S]], BeforeTurnHandler[S]]: ...
    @overload
    def on(
        self, hook: Literal["compact_context"]
    ) -> Callable[[CompactHandler[S]], CompactHandler[S]]: ...
    @overload
    def on(
        self, hook: Literal["before_model_request"]
    ) -> Callable[[RequestHandler[S]], RequestHandler[S]]: ...
    @overload
    def on(
        self, hook: Literal["after_model_response"]
    ) -> Callable[[ResponseHandler[S]], ResponseHandler[S]]: ...
    @overload
    def on(
        self, hook: Literal["before_tool_call"]
    ) -> Callable[[ToolCallHandler[S]], ToolCallHandler[S]]: ...
    @overload
    def on(
        self, hook: Literal["after_tool_result"]
    ) -> Callable[[ToolResultHandler[S]], ToolResultHandler[S]]: ...
    @overload
    def on(self, hook: Literal["on_event"]) -> Callable[[EventHandler[S]], EventHandler[S]]: ...
    def on(self, hook: HookName) -> Callable[[AnyHandler], AnyHandler]:
        self._check_open(f"ext.on({hook!r})")
        if hook not in HOOK_NAMES:
            raise ExtensionDefinitionError(
                f"extension `{self.instance_id}` registered unknown hook `{hook}`. "
                f"Known hooks: {', '.join(sorted(HOOK_NAMES))}."
            )

        def register(handler: AnyHandler) -> AnyHandler:
            self.registrations.handlers.setdefault(hook, []).append(handler)
            return handler

        return register

    @overload
    def tool[A: BaseModel](
        self, name: str, *, args: type[A], description: str, runs_in: Literal["worker"]
    ) -> Callable[[WorkerToolFn[S, A]], WorkerToolFn[S, A]]: ...
    @overload
    def tool[A: BaseModel](
        self, name: str, *, args: type[A], description: str, runs_in: Literal["sandbox"]
    ) -> Callable[[Callable[[A], Exec]], Callable[[A], Exec]]: ...
    def tool(
        self,
        name: str,
        *,
        args: type[BaseModel],
        description: str,
        runs_in: Literal["worker", "sandbox"],
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        self._check_open(f"ext.tool({name!r})")

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool: Tool
            if runs_in == "worker":
                tool = WorkerTool(
                    name=name, description=description, args=args, run=fn, owner=self.instance_id
                )
            else:
                tool = SandboxTool(
                    name=name, description=description, args=args, build=fn, owner=self.instance_id
                )
            self.registrations.tools.append(tool)
            return fn

        return register


@dataclass(frozen=True)
class Extension[C: BaseModel, S: BaseModel]:
    id: str
    api_version: int
    config: type[C]
    state: type[S]
    setup: Callable[[ExtensionAPI[C, S]], None]
    hook_timeout_s: float | None = None


type SetupFn[C: BaseModel, S: BaseModel] = Callable[[ExtensionAPI[C, S]], None]


@overload
def extension[C: BaseModel, S: BaseModel](
    *,
    id: str,
    api_version: int,
    config: type[C],
    state: type[S],
    hook_timeout_s: float | None = None,
) -> Callable[[SetupFn[C, S]], Extension[C, S]]: ...
@overload
def extension[C: BaseModel](
    *, id: str, api_version: int, config: type[C], hook_timeout_s: float | None = None
) -> Callable[[SetupFn[C, NoState]], Extension[C, NoState]]: ...
@overload
def extension[S: BaseModel](
    *, id: str, api_version: int, state: type[S], hook_timeout_s: float | None = None
) -> Callable[[SetupFn[NoConfig, S]], Extension[NoConfig, S]]: ...
@overload
def extension(
    *, id: str, api_version: int, hook_timeout_s: float | None = None
) -> Callable[[SetupFn[NoConfig, NoState]], Extension[NoConfig, NoState]]: ...
def extension(
    *,
    id: str,
    api_version: int,
    config: type[BaseModel] = NoConfig,
    state: type[BaseModel] = NoState,
    hook_timeout_s: float | None = None,
) -> Callable[[SetupFn[Any, Any]], Extension[Any, Any]]:
    """Declares an extension. The decorated function is its setup: it runs once per run, and
    again after takeover or fork, and must register the same hooks for the same config."""
    if not _ID_PATTERN.match(id):
        raise ExtensionDefinitionError(
            f"extension id `{id}` is invalid. Use lowercase dotted names such as `acme.guard`."
        )
    if hook_timeout_s is not None and hook_timeout_s <= 0:
        raise ExtensionDefinitionError(
            f"extension `{id}`: hook_timeout_s must be positive, got {hook_timeout_s}."
        )

    def wrap(setup: SetupFn[Any, Any]) -> Extension[Any, Any]:
        return Extension(
            id=id,
            api_version=api_version,
            config=config,
            state=state,
            setup=setup,
            hook_timeout_s=hook_timeout_s,
        )

    return wrap
