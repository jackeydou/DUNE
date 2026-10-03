"""In-memory stand-ins for Postgres, model-gateway, and sandboxd."""

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from swarmeval.gateway.bus import ChannelSpec
from swarmeval.gateway.model.client import to_wire
from swarmeval.runtime.extensions import (
    CanaryInfo,
    Extension,
    ExtensionUse,
    SandboxCanaryInfo,
    load_extensions,
)
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
    ExtensionSnapshot,
    GatewayRecord,
    MessageDeliverRecord,
    MessageSendRecord,
    ModelCallRecord,
    Transaction,
    Upstream,
    WebExchange,
    WebRequest,
)
from swarmeval.runtime.tools import BUILTIN_TOOL_NAMES, SHELL, WEB_REQUEST
from swarmeval.runtime.writer import RunWriter
from tests.gateway.mock_backend import completion, tool_call


@dataclass
class FakeStore:
    events: list[CommittedEvent] = field(default_factory=list[CommittedEvent])
    generations: dict[str, list[list[ChatMessage]]] = field(
        default_factory=dict[str, list[list[ChatMessage]]]
    )
    agent_states: list[AgentStateRow] = field(default_factory=list[AgentStateRow])
    extension_rows: list[tuple[str, int, ExtensionSnapshot]] = field(
        default_factory=list[tuple[str, int, ExtensionSnapshot]]
    )
    deliveries: dict[tuple[int, str], tuple[str, int | None]] = field(
        default_factory=dict[tuple[int, str], tuple[str, int | None]]
    )
    """(send seq, recipient) → (status, due turn), kept as `runs.deliveries` is."""
    log: list[Transaction] = field(default_factory=list[Transaction])

    async def commit(self, txn: Transaction) -> list[CommittedEvent]:
        self.log.append(txn)
        committed: list[CommittedEvent] = []
        for draft in txn.events:
            seq = len(self.events) + 1
            event = CommittedEvent(
                event_id=draft.event_id,
                seq=seq,
                agent_id=draft.agent_id,
                extension=draft.extension,
                parent_id=draft.parent_id,
                record=draft.record,
            )
            self.events.append(event)
            committed.append(event)
            match draft.record:
                case MessageSendRecord(recipients=recipients):
                    for r in recipients:
                        self.deliveries[(seq, r)] = ("pending", None)
                case MessageDeliverRecord(send_seq=send_seq, recipient=r):
                    status, due = self.deliveries[(send_seq, r)]
                    assert status in ("pending", "delayed"), (send_seq, r, status)
                    self.deliveries[(send_seq, r)] = ("delivered", due)
                case _:
                    pass
        for change in txn.deliveries:
            key = (change.send_seq, change.recipient)
            assert self.deliveries[key][0] == "pending", (key, self.deliveries[key])
            self.deliveries[key] = (change.status, change.due_turn)
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

    async def extension_states(self) -> dict[str, ExtensionSnapshot]:
        return {instance_id: value for instance_id, _, value in self.extension_rows}

    def records(self, kind: str) -> list[CommittedEvent]:
        return [e for e in self.events if e.record.kind == kind]

    def messages(self, agent_id: str) -> list[ChatMessage]:
        return self.generations[agent_id][-1]


GATEWAY = GatewayRecord(
    request_sha256="0" * 64,
    upstream=Upstream(
        backend="scripted",
        model="test-model",
        served_model="test-model",
        reasoning_passback="none",
        weights_hash=None,
        system_fingerprint=None,
        sampling={},
        reasoning_visibility="full",
    ),
    upstream_response_json="{}",
    latency_s=0.0,
    attempts=1,
)
"""A gateway record with placeholder hash and raw response; `honest_record` fills them in."""


def honest_record(request: ModelRequest, response: ModelResponse) -> GatewayRecord:
    """What model-gateway records for a call it served faithfully: the hash of the body the
    worker sends, and a backend completion that normalizes to `response`."""
    body = to_wire(request).model_dump_json(exclude_none=True).encode()
    message = response.message
    raw = completion(
        message.content,
        tool_calls=[tool_call(c.name, c.arguments, id=c.id) for c in message.tool_calls] or None,
        reasoning=message.reasoning,
        prompt_tokens=response.usage.input_tokens,
        completion_tokens=response.usage.output_tokens,
    )
    return GATEWAY.model_copy(
        update={
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "upstream_response_json": json.dumps(raw),
        }
    )


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

    async def generate(
        self, caller: Caller, request: ModelRequest, *, parent_id: str | None
    ) -> RecordedResponse:
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
            gateway=honest_record(request, response),
        )
        draft = EventDraft(
            record=record,
            agent_id=caller.agent_id if isinstance(caller, AgentCaller) else None,
            extension=caller.instance_id if isinstance(caller, ExtensionCaller) else None,
            parent_id=parent_id,
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
    call_ids: list[str] = field(default_factory=list[str])

    async def exec(
        self, sandbox_id: str, os_user: str | None, command: Exec, *, call_id: str
    ) -> ExecResult:
        self.calls.append((sandbox_id, os_user, command))
        self.call_ids.append(call_id)
        return self.handler(command)


@dataclass
class FakePauser:
    """Resumes at once, or when `resume` is set, and keeps each pause's reason."""

    resume: asyncio.Event | None = None
    reasons: list[str] = field(default_factory=list[str])

    async def wait(self, reason: str) -> None:
        self.reasons.append(reason)
        if self.resume is not None:
            await self.resume.wait()


@dataclass
class FakeWeb:
    """Answers every request with `200` and the URL as the body."""

    handler: Callable[[WebRequest], WebExchange] = lambda req: WebExchange(
        request=req, address="93.184.215.14", status=200, body=req.url, body_bytes=len(req.url)
    )
    requests: list[WebRequest] = field(default_factory=list[WebRequest])

    async def request(self, request: WebRequest) -> WebExchange:
        self.requests.append(request)
        return self.handler(request)


def agent(
    id: str = "a", tools: tuple[str, ...] = ("shell",), model: str = "test-model", **kwargs: Any
) -> AgentSpec:
    return AgentSpec(
        id=id,
        model=model,
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
    web: FakeWeb | None
    pauser: "FakePauser"


def harness(
    agents: tuple[AgentSpec, ...],
    scripts: dict[str, list[ModelResponse]],
    *,
    extensions: Sequence[Extension[Any, Any] | tuple[Extension[Any, Any], ExtensionUse]] = (),
    limits: Limits = NO_LIMITS,
    sandbox: FakeSandbox | None = None,
    store: FakeStore | None = None,
    seed: int = 7,
    channels: tuple[ChannelSpec, ...] = (),
    canaries: tuple[CanaryInfo, ...] = (),
    sandbox_canaries: tuple[SandboxCanaryInfo, ...] = (),
    web: FakeWeb | None = None,
    pauser: FakePauser | None = None,
) -> Harness:
    store = store or FakeStore()
    pauser = pauser or FakePauser()
    writer = RunWriter(store)
    model = ScriptedModel(writer, scripts)
    sandbox = sandbox or FakeSandbox()
    exts = [e if isinstance(e, tuple) else (e, ExtensionUse(use=e.id)) for e in extensions]
    loaded = load_extensions(
        [use for _, use in exts],
        builtin_tools=BUILTIN_TOOL_NAMES,
        resolve=resolver(*(e for e, _ in exts)),
    )
    loop = RunLoop(
        RunSpec(
            run_id="run_1",
            seed=seed,
            agents=agents,
            limits=limits,
            channels=channels,
            canaries=canaries,
            sandbox_canaries=sandbox_canaries,
        ),
        writer=writer,
        model_client=model,
        sandbox_executor=sandbox,
        pauser=pauser,
        extensions=loaded,
        tools=[SHELL, WEB_REQUEST],
        web_client=web,
    )
    return Harness(loop=loop, store=store, model=model, sandbox=sandbox, web=web, pauser=pauser)
