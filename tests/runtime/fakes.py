"""In-memory stand-ins for Postgres, model-gateway, and sandboxd."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, JsonValue

from swarmeval.runtime.extensions import Extension, ExtensionUse, load_extensions
from swarmeval.runtime.loop import AgentSpec, Limits, RunLoop, RunSpec
from swarmeval.runtime.messages import (
    AssistantMessage,
    ChatMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    Usage,
)
from swarmeval.runtime.ports import (
    AgentCaller,
    AgentContext,
    Caller,
    ExtensionCaller,
    RecordedResponse,
)
from swarmeval.runtime.records import (
    AgentStateRow,
    CommittedEvent,
    EventDraft,
    Exec,
    ExecResult,
    ModelCallRecord,
    Transaction,
)
from swarmeval.runtime.tools import SandboxTool
from swarmeval.runtime.writer import RunWriter


@dataclass
class FakeStore:
    events: list[CommittedEvent] = field(default_factory=list[CommittedEvent])
    generations: dict[str, list[list[ChatMessage]]] = field(
        default_factory=dict[str, list[list[ChatMessage]]]
    )
    agent_states: list[AgentStateRow] = field(default_factory=list[AgentStateRow])
    extension_rows: list[tuple[str, int, JsonValue]] = field(
        default_factory=list[tuple[str, int, JsonValue]]
    )
    log: list[Transaction] = field(default_factory=list[Transaction])

    async def commit(self, txn: Transaction) -> list[CommittedEvent]:
        self.log.append(txn)
        committed: list[CommittedEvent] = []
        for draft in txn.events:
            seq = len(self.events) + 1
            event = CommittedEvent(
                event_id=f"evt_{seq}",
                seq=seq,
                agent_id=draft.agent_id,
                extension=draft.extension,
                parent_id=draft.parent_id,
                record=draft.record,
            )
            self.events.append(event)
            committed.append(event)
        for agent_id, messages in txn.new_generations.items():
            self.generations.setdefault(agent_id, []).append(list(messages))
        for agent_id, message in txn.messages:
            self.generations[agent_id][-1].append(message)
        self.agent_states.extend(txn.agent_states)
        for instance_id, value in txn.extension_states.items():
            self.extension_rows.append((instance_id, len(self.events), value))
        return committed

    async def context(self, agent_id: str) -> AgentContext | None:
        gens = self.generations.get(agent_id)
        if not gens:
            return None
        row = [r for r in self.agent_states if r.agent_id == agent_id][-1]
        return AgentContext(gen=row.gen, messages=tuple(gens[row.gen][: row.length]))

    async def extension_states(self) -> dict[str, JsonValue]:
        return {instance_id: value for instance_id, _, value in self.extension_rows}

    def records(self, kind: str) -> list[CommittedEvent]:
        return [e for e in self.events if e.record.kind == kind]

    def messages(self, agent_id: str) -> list[ChatMessage]:
        return self.generations[agent_id][-1]


def reply(content: str = "", *tool_calls: ToolCall, tokens: int = 10) -> ModelResponse:
    return ModelResponse(
        message=AssistantMessage(content=content, tool_calls=tuple(tool_calls)),
        usage=Usage(input_tokens=tokens, output_tokens=0),
    )


def call(name: str, arguments: str = "{}", id: str | None = None) -> ToolCall:
    return ToolCall(id=id or f"call_{name}", name=name, arguments=arguments)


@dataclass
class ScriptedModel:
    """Replays scripted responses per caller and records them the way the gateway path does."""

    writer: RunWriter
    scripts: dict[str, list[ModelResponse]]
    requests: list[tuple[Caller, ModelRequest]] = field(
        default_factory=list[tuple[Caller, ModelRequest]]
    )

    async def generate(self, caller: Caller, request: ModelRequest) -> RecordedResponse:
        self.requests.append((caller, request))
        key = caller.agent_id if isinstance(caller, AgentCaller) else caller.instance_id
        response = self.scripts[key].pop(0)
        record = ModelCallRecord(
            model=request.model,
            gen=request.gen,
            length=None if request.gen is None else len(request.messages),
            options=request.options,
            response=response.message,
            usage=response.usage,
        )
        draft = EventDraft(
            record=record,
            agent_id=caller.agent_id if isinstance(caller, AgentCaller) else None,
            extension=caller.instance_id if isinstance(caller, ExtensionCaller) else None,
        )
        (event,) = await self.writer.commit(Transaction(events=[draft]))
        return RecordedResponse(response=response, event=event)


@dataclass
class FakeSandbox:
    handler: Callable[[Exec], ExecResult] = lambda cmd: ExecResult(
        exit_code=0, stdout=" ".join(cmd.argv), stderr=""
    )
    calls: list[tuple[str, str | None, Exec]] = field(
        default_factory=list[tuple[str, str | None, Exec]]
    )

    async def exec(self, sandbox_id: str, os_user: str | None, command: Exec) -> ExecResult:
        self.calls.append((sandbox_id, os_user, command))
        return self.handler(command)


class ShellArgs(BaseModel):
    cmd: str


def _build_shell(args: ShellArgs) -> Exec:
    return Exec(argv=("sh", "-c", args.cmd))


SHELL = SandboxTool(
    name="shell", description="Run a shell command.", args=ShellArgs, build=_build_shell
)


def agent(id: str = "a", tools: tuple[str, ...] = ("shell",), **kwargs: Any) -> AgentSpec:
    return AgentSpec(
        id=id,
        model="test-model",
        system_prompt=f"You are {id}.",
        task="Do the task.",
        tools=tools,
        sandbox_id=f"box_{id}",
        **kwargs,
    )


def resolver(*extensions: Extension[Any, Any]) -> Callable[[str], Extension[Any, Any]]:
    by_id = {e.id: e for e in extensions}
    return lambda name: by_id[name]


NO_LIMITS = Limits()


@dataclass
class Harness:
    loop: RunLoop
    store: FakeStore
    model: ScriptedModel
    sandbox: FakeSandbox


def harness(
    agents: tuple[AgentSpec, ...],
    scripts: dict[str, list[ModelResponse]],
    *,
    extensions: Sequence[Extension[Any, Any] | tuple[Extension[Any, Any], ExtensionUse]] = (),
    limits: Limits = NO_LIMITS,
    sandbox: FakeSandbox | None = None,
    store: FakeStore | None = None,
    seed: int = 7,
) -> Harness:
    store = store or FakeStore()
    writer = RunWriter(store)
    model = ScriptedModel(writer, scripts)
    sandbox = sandbox or FakeSandbox()
    exts = [e if isinstance(e, tuple) else (e, ExtensionUse(use=e.id)) for e in extensions]
    loaded = load_extensions(
        [use for _, use in exts], builtin_tools=["shell"], resolve=resolver(*(e for e, _ in exts))
    )
    loop = RunLoop(
        RunSpec(run_id="run_1", seed=seed, agents=agents, limits=limits),
        writer=writer,
        model_client=model,
        sandbox_executor=sandbox,
        extensions=loaded,
        tools=[SHELL],
    )
    return Harness(loop=loop, store=store, model=model, sandbox=sandbox)
