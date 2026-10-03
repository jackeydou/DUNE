"""What the loop commits: event records, committed events, and transactions.

A record says what happened; the events package turns it into an Inspect `Event` when it is
written (see docs/event-log.md). The runtime never builds Inspect objects itself.
"""

import uuid
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from swarmeval.runtime.messages import (
    AssistantMessage,
    ChatMessage,
    Frozen,
    RequestOptions,
    ToolCall,
    Usage,
)

HookName = Literal[
    "on_run_start",
    "on_resume",
    "before_turn",
    "compact_context",
    "before_model_request",
    "after_model_response",
    "before_tool_call",
    "after_tool_result",
    "before_deliver",
    "after_turn",
    "on_event",
    "on_run_end",
]


class ToolResult(Frozen):
    """A tool's outcome as the agent will see it, before `after_tool_result` hooks."""

    call_id: str
    tool: str
    content: str
    is_error: bool = False


class Exec(Frozen):
    """A command for sandboxd to run inside a sandbox."""

    argv: tuple[str, ...]
    cwd: str | None = None
    timeout_s: float = 60.0


class FsChange(Frozen):
    path: str
    op: Literal["create", "modify", "delete"]
    kind: Literal["file", "dir", "symlink", "other"] = "file"
    uid: int
    """Owner after the change; before it for a delete."""
    mode: int = 0
    size: int = 0
    before_sha256: str | None
    after_sha256: str | None
    protected: bool = False
    attribution: Literal["call", "ambiguous"] = "call"
    candidate_calls: tuple[str, ...] = ()
    """For an ambiguous change: calls whose processes were alive in the window."""
    content_stored: bool = False
    """The new content is in the blob store under `after_sha256`."""


class ProcessInfo(Frozen):
    pid: int
    ppid: int
    user: str
    cmdline: str


class Truncated(Frozen):
    """Output longer than what the agent is shown."""

    size: int
    """Bytes the command produced in total."""
    sha256: str
    """Blob holding the kept output."""
    capped: bool = False
    """The kept output is itself cut at sandboxd's blob limit."""


class ExecResult(Frozen):
    exit_code: int
    stdout: str
    """The first bytes of output, up to sandboxd's inline limit. What the agent is shown."""
    stderr: str
    timed_out: bool = False
    duration_s: float | None = None
    stdout_truncated: Truncated | None = None
    stderr_truncated: Truncated | None = None
    fs_changes: tuple[FsChange, ...] = ()
    background_changes: tuple[FsChange, ...] = ()
    """Changed between the previous call on this sandbox and this one, by background
    processes. Always ambiguous."""
    processes: tuple[ProcessInfo, ...] = ()


class WebRequest(Frozen):
    """One `web_request` as the agent asked for it, after argument validation."""

    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    body: str | None = None
    timeout_s: float = 30.0


class WebExchange(Frozen):
    """What the worker did for one `web_request` and what came back. Bodies are in the blob
    store under their hashes; only `body`, the part the agent is shown, is inline."""

    request: WebRequest
    request_body_sha256: str | None = None
    address: str | None = None
    """The address connected to, after the public-address check."""
    refused: str | None = None
    """Why the request was never sent, when the address check failed. The agent sees only a
    connection error."""
    error: str | None = None
    """A failure after the check: DNS, connect, TLS, timeout, or a broken response."""
    status: int | None = None
    response_headers: tuple[tuple[str, str], ...] = ()
    response_body_size: int = 0
    """Bytes of body received, after content decoding."""
    response_body_sha256: str | None = None
    response_body_capped: bool = False
    """Reading stopped at the worker's body cap; the blob holds the bytes up to it."""
    body: str = ""
    """The body's first `body_bytes` bytes as text. What the agent is shown."""
    body_bytes: int = 0
    duration_s: float | None = None


class Upstream(Frozen):
    """What model-gateway sent to, and learned from, the backend."""

    backend: str
    model: str
    """Model name the backend was asked for."""
    served_model: str
    """Model name or version the backend reported."""
    reasoning_passback: Literal["none", "within_turn", "all"]
    weights_hash: str | None
    system_fingerprint: str | None
    sampling: dict[str, JsonValue]
    """Sampling parameters actually sent: the request's, merged over the backend's defaults."""
    reasoning_visibility: Literal["full"]


class GatewayRecord(Frozen):
    """model-gateway's account of one call, beyond the response itself."""

    request_sha256: str
    """sha256 of the HTTP request body as the gateway received it."""
    upstream: Upstream
    upstream_response_json: str
    """The backend's response body before normalization, as JSON text."""
    latency_s: float
    attempts: int
    """HTTP attempts, retries included."""


class ModelCallRecord(Frozen):
    """Written by the model-gateway path, not by the loop."""

    kind: Literal["model"] = "model"
    model: str
    gen: int | None
    length: int | None
    options: RequestOptions
    response: AssistantMessage
    usage: Usage
    gateway: GatewayRecord


class ToolCallRecord(Frozen):
    kind: Literal["tool"] = "tool"
    call: ToolCall
    executed_arguments: str | None
    """What actually ran. `None` when the call was blocked or never reached execution."""
    result: ToolResult
    blocked_by: str | None = None
    exec_result: ExecResult | None = None
    web: WebExchange | None = None


class SandboxExecRecord(Frozen):
    """A command an extension ran in a sandbox through `ctx.sandbox`."""

    kind: Literal["sandbox_exec"] = "sandbox_exec"
    sandbox_id: str
    command: Exec
    result: ExecResult


ProbeName = Literal["interfaces", "connect", "dns", "shared_path", "proc"]
ProbeOutcome = Literal["isolated", "leaked", "unverified"]


class ProbeFinding(Frozen):
    """What one isolation probe found, as seen from the sandbox it ran in."""

    probe: ProbeName
    peer: str | None = None
    """The other sandbox, for a probe between two: its marker or name was looked for here."""
    outcome: ProbeOutcome
    """`unverified` when the probe could not run, e.g. the image lacks every tool it tries."""
    detail: str


class IsolationProbeRecord(Frozen):
    """One command of the isolation self-check, run by the worker in a sandbox after the run's
    sandboxes exist and before any agent turn. `plant` leaves markers, `check` looks for the
    other sandboxes' markers and names and for a way out, `clean` removes the markers."""

    kind: Literal["isolation_probe"] = "isolation_probe"
    sandbox_id: str
    step: Literal["plant", "check", "clean"]
    command: Exec
    result: ExecResult
    findings: tuple[ProbeFinding, ...] = ()


class InterventionRecord(Frozen):
    kind: Literal["intervention"] = "intervention"
    hook: HookName | Literal["tool", "fork"]
    """`fork`: an edit a fork made to what its source left (docs/services/orchestrator.md#forks)."""
    action: str
    target_event_id: str | None
    before_sha256: str | None
    after: JsonValue


class ExtensionEmitRecord(Frozen):
    kind: Literal["extension"] = "extension"
    name: str
    data: JsonValue


class AlertRecord(Frozen):
    kind: Literal["alert"] = "alert"
    message: str
    severity: Literal["low", "medium", "high"]
    event_ids: tuple[str, ...] = ()


class MessageSendRecord(Frozen):
    """An agent sent a message on a channel. One delivery per recipient follows."""

    kind: Literal["msg.send"] = "msg.send"
    channel: str
    sender: str
    content: str
    recipients: tuple[str, ...]
    call_id: str | None
    """The `send_message` tool call that sent it; `None` for a message an extension posted on
    the channel (`ctx.actions.post`), whose event names the extension instead of an agent."""


class MessageDeliverRecord(Frozen):
    """A message entered a recipient's context. `content` is what the recipient saw, which
    differs from the sent original only when a `before_deliver` intervention rewrote it."""

    kind: Literal["msg.deliver"] = "msg.deliver"
    channel: str
    sender: str
    recipient: str
    send_seq: int
    content: str


class FinalDiffRecord(Frozen):
    """sandboxd's last diff of a sandbox after the agents stopped: writes by background
    processes since the last call. Every change is ambiguous."""

    kind: Literal["final_diff"] = "final_diff"
    sandbox_id: str
    changes: tuple[FsChange, ...]


class ScoreRecord(Frozen):
    """A final-state scorer's verdict. `value` is 1 when what the scorer looks for happened
    (`meaning` says what that is), never a correctness grade."""

    kind: Literal["score"] = "score"
    scorer: str
    value: Literal[0, 1]
    meaning: str
    explanation: str
    event_ids: tuple[str, ...] = ()
    """Events that are the evidence for the verdict."""


class LimitRecord(Frozen):
    kind: Literal["limit"] = "limit"
    limit: Literal["max_turns", "max_tokens"]
    value: int


class LifecycleRecord(Frozen):
    kind: Literal["lifecycle"] = "lifecycle"
    status: Literal["started", "paused", "resumed", "finished", "stopped", "limit", "failed"]
    """`paused` and `resumed` mark a pause in the middle of the run; the last lifecycle event
    is its outcome."""
    reason: str | None = None
    hook: HookName | Literal["tool", "spawn"] | None = None
    error: str | None = None


class TranscriptMismatch(Frozen):
    check: Literal["context", "request", "response", "send", "delivery"]
    """`context`: a message in an agent's context that no recorded event (or intervention on it)
    explains. `request`: a model call whose request, rebuilt from the stored context, does not
    hash to what model-gateway received. `response`: a model call whose response differs from
    the backend's raw response, normalized again. `send`: a `msg.send` that neither a
    `send_message` call of its model event nor an extension's post explains. `delivery`: a
    `msg.deliver` whose content is neither what was sent nor what a `before_deliver` rewrite
    made of it, or that delivers a message dropped for its recipient."""
    agent_id: str | None
    gen: int | None
    idx: int | None
    """The context message's index; for `request`, the length of the context the call used."""
    event_id: str | None
    """The event the message or call was compared with; `None` when no event could explain it."""
    detail: str


class TranscriptCheckRecord(Frozen):
    """The worker's comparison, at run end, of what the agents saw with what the gateway, the
    tools, and the Message Bus recorded. A difference an intervention explains is an
    intervention; any other is a mismatch, the spoofing signal (v1 spec §6)."""

    kind: Literal["transcript_check"] = "transcript_check"
    consistent: bool
    messages: int
    """Context messages compared."""
    requests: int
    """Agent model calls whose request hash was recomputed from the stored context."""
    responses: int
    """Model calls whose response was compared with the backend's raw response."""
    deliveries: int = 0
    """`msg.deliver` events compared with their sends. Absent before event schema version 5."""
    interventions: tuple[str, ...]
    """Intervention events that explained a context message differing from its source."""
    mismatches: tuple[TranscriptMismatch, ...]
    event_ids: tuple[str, ...]
    """Every event a mismatch names, once."""


Record = Annotated[
    ModelCallRecord
    | ToolCallRecord
    | SandboxExecRecord
    | IsolationProbeRecord
    | InterventionRecord
    | ExtensionEmitRecord
    | AlertRecord
    | MessageSendRecord
    | MessageDeliverRecord
    | FinalDiffRecord
    | ScoreRecord
    | LimitRecord
    | LifecycleRecord
    | TranscriptCheckRecord,
    Field(discriminator="kind"),
]


class CommittedEvent(Frozen):
    event_id: str
    seq: int
    agent_id: str | None
    extension: str | None
    """Instance id of the extension that caused this event, if any."""
    parent_id: str | None
    record: Record


def new_event_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class EventDraft:
    """An event before it commits. Its id is fixed here, so events in one transaction can name
    each other as parents (docs/event-log.md#causal-parents)."""

    record: Record
    agent_id: str | None = None
    extension: str | None = None
    parent_id: str | None = None
    event_id: str = field(default_factory=new_event_id)


@dataclass(frozen=True)
class AgentStateRow:
    agent_id: str
    gen: int
    length: int
    turn: int
    status: Literal["awaiting_admit", "ready", "finished"]
    tokens_used: int


@dataclass(frozen=True)
class DeliveryChange:
    """What `before_deliver` decided for one recipient of a send, when it was not to deliver at
    the recipient's next turn. Recorded in `runs.deliveries` with the interventions that made
    it."""

    send_seq: int
    recipient: str
    status: Literal["dropped", "delayed"]
    due_turn: int | None = None
    """For `delayed`: the recipient's own turn at whose start the message is delivered."""


class AgentCheckpoint(Frozen):
    gen: int
    length: int
    turn: int
    """The agent's own turns started."""
    finished: bool
    last_input: str
    """The parent of the agent's next model call."""


class MailCheckpoint(Frozen):
    """A routed message not yet delivered: due at the recipient's next turn, or held."""

    recipient: str
    send_seq: int
    send_event_id: str
    send: MessageSendRecord
    content: str
    """What will be delivered, after `before_deliver`."""
    due_turn: int | None
    parent_id: str


class QueuedCheckpoint(Frozen):
    """An injection or a post an extension asked for, not yet taken by the loop."""

    event_id: str
    content: str
    agent_id: str | None = None
    """For an injection."""
    instance_id: str | None = None
    """For a post: the extension that posted."""
    channel: str | None = None
    sender: str | None = None
    """For a post."""


class ExtensionCheckpoint(Frozen):
    state: JsonValue
    rng_uses: int


class Checkpoint(Frozen):
    """The loop's state at the start of a run-wide turn, once the observers have caught up:
    everything a fork needs besides the messages, which `agents` locate by generation and
    length. Committed into `runs.checkpoints` with the seq of the last event before the turn."""

    turn: int
    """Run-wide turns started before this one."""
    round: tuple[str, ...]
    """Agents left in the current `round_robin` round, the one about to step first."""
    tokens_used: int
    agents: dict[str, AgentCheckpoint]
    extensions: dict[str, ExtensionCheckpoint]
    mail: tuple[MailCheckpoint, ...] = ()
    queued: tuple[QueuedCheckpoint, ...] = ()


@dataclass(frozen=True)
class ExtensionSnapshot:
    """An extension instance's committed state, and how many of its hook calls have drawn from
    `ctx.rng`, which fixes the random stream its next call gets."""

    state: JsonValue
    rng_uses: int = 0


@dataclass
class Transaction:
    """One atomic commit. `messages` are appended to each agent's current generation, after
    `new_generations` (if any) has started a fresh one."""

    events: list[EventDraft] = field(default_factory=list[EventDraft])
    new_generations: dict[str, tuple[ChatMessage, ...]] = field(
        default_factory=dict[str, tuple[ChatMessage, ...]]
    )
    messages: list[tuple[str, ChatMessage]] = field(default_factory=list[tuple[str, ChatMessage]])
    agent_states: list[AgentStateRow] = field(default_factory=list[AgentStateRow])
    extension_states: dict[str, ExtensionSnapshot] = field(
        default_factory=dict[str, ExtensionSnapshot]
    )
    deliveries: list[DeliveryChange] = field(default_factory=list[DeliveryChange])
    checkpoint: Checkpoint | None = None
    inherited: dict[tuple[str, int], tuple[ChatMessage, ...]] = field(
        default_factory=dict[tuple[str, int], tuple[ChatMessage, ...]]
    )
    """A fork's copy of its source's contexts: `(agent, gen)` to the messages, written at those
    generation numbers, before `new_generations`."""
    inherited_mail: list[MailCheckpoint] = field(default_factory=list[MailCheckpoint])
    """Mail a fork carries over; opens its `runs.deliveries` rows as its source had them."""

    def extend(self, other: "Transaction") -> None:
        self.events.extend(other.events)
        self.new_generations.update(other.new_generations)
        self.messages.extend(other.messages)
        self.agent_states.extend(other.agent_states)
        self.extension_states.update(other.extension_states)
        self.deliveries.extend(other.deliveries)
        assert other.checkpoint is None and not other.inherited and not other.inherited_mail, (
            "only the loop commits checkpoints and a fork's start, never in an extended txn"
        )

    def is_empty(self) -> bool:
        return not (
            self.events
            or self.new_generations
            or self.messages
            or self.agent_states
            or self.extension_states
            or self.deliveries
            or self.checkpoint
            or self.inherited
            or self.inherited_mail
        )
