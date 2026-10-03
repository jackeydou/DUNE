"""What a detector reads of one event (M2 spec decision 6): who and where, the text an agent
produced or was shown, the file changes and processes sandboxd saw, and the channel of a message.

Two adapters build it. `view_of` reads a `CommittedEvent` in the worker, as the online Monitor
and the `rule` scorer see events; `view_of_row` reads a row of a run's `events.parquet`, as
offline analysis does. Both give the same view of the same event.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from pydantic import JsonValue

from swarmeval.runtime.records import (
    CommittedEvent,
    ExecResult,
    FinalDiffRecord,
    FsChange,
    InterventionRecord,
    IsolationProbeRecord,
    MessageDeliverRecord,
    MessageSendRecord,
    ModelCallRecord,
    ProcessInfo,
    SandboxExecRecord,
    ToolCallRecord,
)

TextRole = Literal["model_output", "tool_output", "message", "rewritten_message", "delivered"]
"""`model_output`: a model call's content, reasoning, and tool call arguments. `message`: what a
sender put on a channel. `rewritten_message`: a delivery's content a `before_deliver`
intervention replaced, as its recipient read it. `delivered`: a delivery's content, which
repeats its send unless rewritten."""

TEXT_ROLES: tuple[TextRole, ...] = (
    "model_output",
    "tool_output",
    "message",
    "rewritten_message",
    "delivered",
)


@dataclass(frozen=True)
class Text:
    role: TextRole
    field: str
    """Where in the event, e.g. `content`, `reasoning`, `tool_calls.<id>.arguments`."""
    text: str


@dataclass(frozen=True)
class Change:
    sandbox_id: str | None
    """`None` when the event's agent has no known sandbox."""
    change: FsChange


@dataclass(frozen=True)
class EventView:
    event_id: str
    seq: int
    kind: str
    """The record kind: `model`, `tool`, `msg.send`, `msg.deliver`, `intervention`,
    `sandbox_exec`, `isolation_probe`, `final_diff`, `extension`, `alert`, `score`, `limit`,
    `lifecycle`, `transcript_check`."""
    agent_id: str | None
    extension: str | None
    sandbox_id: str | None
    """Where a tool or sandbox command ran."""
    texts: tuple[Text, ...] = ()
    changes: tuple[Change, ...] = ()
    processes: tuple[ProcessInfo, ...] = ()
    tool: str | None = None
    channel: str | None = None
    sender: str | None = None
    recipients: tuple[str, ...] = ()
    """A send's recipients; the one recipient of a delivery or a delivery rewrite."""

    def texts_in(self, roles: Sequence[TextRole]) -> list[Text]:
        return [t for t in self.texts if t.role in roles]


def view_of(event: CommittedEvent, sandboxes: Mapping[str, str | None]) -> EventView:
    """`sandboxes` maps each agent to its sandbox, to place a tool call's file changes."""
    head = EventView(
        event_id=event.event_id,
        seq=event.seq,
        kind=event.record.kind,
        agent_id=event.agent_id,
        extension=event.extension,
        sandbox_id=None,
    )
    match event.record:
        case ModelCallRecord(response=response):
            texts = [Text("model_output", "content", response.content)]
            if response.reasoning:
                texts.append(Text("model_output", "reasoning", response.reasoning))
            texts += [
                Text("model_output", f"tool_calls.{c.id}.arguments", c.arguments)
                for c in response.tool_calls
            ]
            return replace(head, texts=tuple(texts))
        case ToolCallRecord(call=call, result=result, exec_result=exec_result):
            sandbox = sandboxes.get(event.agent_id) if event.agent_id else None
            texts = (Text("tool_output", "result", result.content),)
            if exec_result is None:
                return replace(head, texts=texts, tool=call.name)
            return replace(
                head,
                sandbox_id=sandbox,
                texts=texts,
                tool=call.name,
                changes=_changes(sandbox, exec_result),
                processes=exec_result.processes,
            )
        case (
            SandboxExecRecord(sandbox_id=sandbox, result=result)
            | IsolationProbeRecord(sandbox_id=sandbox, result=result)
        ):
            return replace(
                head,
                sandbox_id=sandbox,
                changes=_changes(sandbox, result),
                processes=result.processes,
            )
        case FinalDiffRecord(sandbox_id=sandbox, changes=changes):
            return replace(
                head,
                sandbox_id=sandbox,
                changes=tuple(Change(sandbox, c) for c in changes),
            )
        case MessageSendRecord(channel=channel, sender=sender, content=content):
            return replace(
                head,
                texts=(Text("message", "content", content),),
                channel=channel,
                sender=sender,
                recipients=event.record.recipients,
            )
        case MessageDeliverRecord(channel=channel, sender=sender, recipient=recipient):
            return replace(
                head,
                texts=(Text("delivered", "content", event.record.content),),
                channel=channel,
                sender=sender,
                recipients=(recipient,),
            )
        case InterventionRecord(
            hook="before_deliver", after={"recipient": str(recipient), "content": str(content)}
        ):
            return replace(
                head,
                texts=(Text("rewritten_message", "after.content", content),),
                recipients=(recipient,),
            )
        case _:
            return head


def _changes(sandbox: str | None, result: ExecResult) -> tuple[Change, ...]:
    return tuple(Change(sandbox, c) for c in (*result.background_changes, *result.fs_changes))


_KINDS = {"model": "model", "tool": "tool", "score": "score", "sample_limit": "limit"}


def view_of_row(row: Mapping[str, object]) -> EventView:
    """A row of `events.parquet`: `event_id`, `seq`, `type`, `agent_id`, `sandbox_id`, and
    `payload`, the stored Inspect event as JSON text."""
    payload: dict[str, Any] = json.loads(str(row["payload"]))
    ours: dict[str, Any] = payload.get("metadata", {}).get("swarmeval", {})
    type_ = str(row["type"])
    sandbox = _str_or_none(row["sandbox_id"])
    agent = _str_or_none(row["agent_id"])
    seq = row["seq"]
    assert isinstance(seq, int), "events.parquet types seq as int64"
    kind = _KINDS.get(type_) or type_.removeprefix("swarmeval.")
    if type_ == "sandbox":
        kind = "isolation_probe" if "probe" in ours else "sandbox_exec"
    head = EventView(
        event_id=str(row["event_id"]),
        seq=seq,
        kind=kind,
        agent_id=agent,
        extension=ours.get("extension"),
        sandbox_id=None,
    )
    data: dict[str, Any] = payload.get("data") or {}
    match kind:
        case "model":
            return replace(head, texts=tuple(_model_texts(payload, ours)))
        case "tool":
            texts = (Text("tool_output", "result", str(payload.get("result", ""))),)
            exec_: dict[str, Any] | None = ours.get("exec")
            if exec_ is None:
                return replace(head, texts=texts, tool=payload["function"])
            return replace(
                head,
                sandbox_id=sandbox,
                texts=texts,
                tool=payload["function"],
                changes=_stored_changes(sandbox, exec_),
                processes=_processes(exec_),
            )
        case "sandbox_exec" | "isolation_probe":
            stored: dict[str, Any] = ours["exec"]
            return replace(
                head,
                sandbox_id=sandbox,
                changes=_stored_changes(sandbox, stored),
                processes=_processes(stored),
            )
        case "final_diff":
            sandbox = data["sandbox_id"]
            return replace(
                head,
                sandbox_id=sandbox,
                changes=tuple(Change(sandbox, FsChange.model_validate(c)) for c in data["changes"]),
            )
        case "msg.send":
            return replace(
                head,
                texts=(Text("message", "content", data["content"]),),
                channel=data["channel"],
                sender=data["sender"],
                recipients=tuple(data["recipients"]),
            )
        case "msg.deliver":
            return replace(
                head,
                texts=(Text("delivered", "content", data["content"]),),
                channel=data["channel"],
                sender=data["sender"],
                recipients=(data["recipient"],),
            )
        case "intervention":
            after: JsonValue = data.get("after")
            if (
                data.get("hook") == "before_deliver"
                and isinstance(after, dict)
                and isinstance(after.get("content"), str)
                and isinstance(after.get("recipient"), str)
            ):
                return replace(
                    head,
                    texts=(Text("rewritten_message", "after.content", str(after["content"])),),
                    recipients=(str(after["recipient"]),),
                )
            return head
        case _:
            return head


def _model_texts(payload: dict[str, Any], ours: dict[str, Any]) -> list[Text]:
    message: dict[str, Any] = payload["output"]["choices"][0]["message"]
    content: str | list[dict[str, Any]] = message["content"]
    texts: list[Text] = []
    if isinstance(content, str):
        texts.append(Text("model_output", "content", content))
    else:
        text = "".join(c["text"] for c in content if c["type"] == "text")
        texts.append(Text("model_output", "content", text))
        texts += [
            Text("model_output", "reasoning", c["reasoning"])
            for c in content
            if c["type"] == "reasoning" and c["reasoning"]
        ]
    raw: dict[str, str] = ours.get("raw_tool_arguments", {})
    texts += [Text("model_output", f"tool_calls.{i}.arguments", a) for i, a in raw.items()]
    return texts


def _stored_changes(sandbox: str | None, exec_: dict[str, Any]) -> tuple[Change, ...]:
    changes = [*exec_.get("background_changes", ()), *exec_.get("fs_changes", ())]
    return tuple(Change(sandbox, FsChange.model_validate(c)) for c in changes)


def _processes(exec_: dict[str, Any]) -> tuple[ProcessInfo, ...]:
    return tuple(ProcessInfo.model_validate(p) for p in exec_.get("processes", ()))


def _str_or_none(value: object) -> str | None:
    return None if value is None else str(value)
