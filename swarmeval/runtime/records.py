"""What the loop commits: event records, committed events, and transactions.

A record says what happened; the events package turns it into an Inspect `Event` when it is
written (see docs/event-log.md). The runtime never builds Inspect objects itself.
"""

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
    "before_turn",
    "compact_context",
    "before_model_request",
    "after_model_response",
    "before_tool_call",
    "after_tool_result",
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


class SandboxExecRecord(Frozen):
    """A command an extension ran in a sandbox through `ctx.sandbox`."""

    kind: Literal["sandbox_exec"] = "sandbox_exec"
    sandbox_id: str
    command: Exec
    result: ExecResult


class InterventionRecord(Frozen):
    kind: Literal["intervention"] = "intervention"
    hook: HookName | Literal["tool"]
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
    call_id: str
    """The `send_message` tool call that sent it."""


class MessageDeliverRecord(Frozen):
    """A message entered a recipient's context. `content` is what the recipient saw, which
    differs from the sent original only when an intervention applies."""

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
    status: Literal["started", "finished", "stopped", "limit", "failed"]
    reason: str | None = None
    hook: HookName | Literal["tool", "spawn"] | None = None
    error: str | None = None


Record = Annotated[
    ModelCallRecord
    | ToolCallRecord
    | SandboxExecRecord
    | InterventionRecord
    | ExtensionEmitRecord
    | AlertRecord
    | MessageSendRecord
    | MessageDeliverRecord
    | FinalDiffRecord
    | ScoreRecord
    | LimitRecord
    | LifecycleRecord,
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


@dataclass(frozen=True)
class EventDraft:
    record: Record
    agent_id: str | None = None
    extension: str | None = None
    parent_id: str | None = None


@dataclass(frozen=True)
class AgentStateRow:
    agent_id: str
    gen: int
    length: int
    turn: int
    status: Literal["awaiting_admit", "ready", "finished"]
    tokens_used: int


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
    extension_states: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])

    def extend(self, other: "Transaction") -> None:
        self.events.extend(other.events)
        self.new_generations.update(other.new_generations)
        self.messages.extend(other.messages)
        self.agent_states.extend(other.agent_states)
        self.extension_states.update(other.extension_states)

    def is_empty(self) -> bool:
        return not (
            self.events
            or self.new_generations
            or self.messages
            or self.agent_states
            or self.extension_states
        )
