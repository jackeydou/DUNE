"""A fork's start: the source's state at a turn boundary, and the edits a person asked for
(M2 spec decisions 8 and 9, docs/services/orchestrator.md#forks).

Edits are validated twice: by the control plane when the fork is requested, against the
checkpoint and contexts it read, and here again when the worker starts the fork.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, NonNegativeInt, TypeAdapter

from swarmeval.runtime.messages import (
    AssistantMessage,
    ChatMessage,
    Frozen,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from swarmeval.runtime.records import Checkpoint, EventDraft, InterventionRecord, MailCheckpoint


class ForkError(Exception):
    """An edit does not fit the state it is applied to."""


class ReplaceMessage(Frozen):
    """Replace the text of message `index` in an agent's context."""

    kind: Literal["replace_message"] = "replace_message"
    agent_id: str
    index: NonNegativeInt
    content: str


class DeleteMessage(Frozen):
    """Remove user message `index` (a delivery or an injection) from an agent's context."""

    kind: Literal["delete_message"] = "delete_message"
    agent_id: str
    index: NonNegativeInt


class ReplaceDelivery(Frozen):
    """Deliver other content for a message routed to `recipient` but not yet delivered."""

    kind: Literal["replace_delivery"] = "replace_delivery"
    send_event_id: str
    recipient: str
    content: str


type Edit = Annotated[ReplaceMessage | DeleteMessage | ReplaceDelivery, Field(discriminator="kind")]
EDITS = TypeAdapter[tuple[Edit, ...]](tuple[Edit, ...])


@dataclass(frozen=True)
class ForkStart:
    source_run_id: str
    fork_seq: int
    checkpoint: Checkpoint
    """With every event id in it that names a source event written `run:event_id`."""
    contexts: Mapping[str, tuple[ChatMessage, ...]]
    """Each agent's context at the checkpoint: its generation `gen`, cut to `length`."""
    edits: tuple[Edit, ...]
    fidelity: Literal["fs_restored", "fs_partial"]


def cross_run(run_id: str, event_id: str) -> str:
    """A reference to `event_id` of run `run_id` from another run; one that already names its
    run (a fork of a fork) stays."""
    return event_id if ":" in event_id else f"{run_id}:{event_id}"


def from_source(checkpoint: Checkpoint, source_run_id: str) -> Checkpoint:
    """The checkpoint with every event id it holds written as a reference into the source."""

    def ref(event_id: str) -> str:
        return cross_run(source_run_id, event_id)

    return checkpoint.model_copy(
        update={
            "agents": {
                a: s.model_copy(update={"last_input": ref(s.last_input)})
                for a, s in checkpoint.agents.items()
            },
            "mail": tuple(
                m.model_copy(
                    update={"send_event_id": ref(m.send_event_id), "parent_id": ref(m.parent_id)}
                )
                for m in checkpoint.mail
            ),
            "queued": tuple(
                q.model_copy(update={"event_id": ref(q.event_id)}) for q in checkpoint.queued
            ),
        }
    )


def check_edits(
    edits: Sequence[Edit],
    contexts: Mapping[str, tuple[ChatMessage, ...]],
    mail: Sequence[MailCheckpoint],
) -> None:
    """Raises `ForkError` naming the edit that does not fit."""
    for i, edit in enumerate(edits):
        where = f"edit {i} ({edit.kind})"
        match edit:
            case ReplaceMessage() | DeleteMessage():
                context = contexts.get(edit.agent_id)
                if context is None:
                    raise ForkError(
                        f"{where}: no agent `{edit.agent_id}`. Agents: {', '.join(contexts)}."
                    )
                if edit.index >= len(context):
                    raise ForkError(
                        f"{where}: agent `{edit.agent_id}` has {len(context)} messages at the "
                        f"fork point; index {edit.index} is past them."
                    )
                message = context[edit.index]
                if isinstance(edit, DeleteMessage) and not isinstance(message, UserMessage):
                    raise ForkError(
                        f"{where}: message {edit.index} of `{edit.agent_id}` is a "
                        f"{message.role} message; only user messages (deliveries, injections) "
                        "can be deleted without breaking the conversation. Replace its text."
                    )
            case ReplaceDelivery():
                if not any(_is(m, edit) for m in mail):
                    pending = ", ".join(f"{m.send_event_id} → {m.recipient}" for m in mail)
                    raise ForkError(
                        f"{where}: message {edit.send_event_id} has no undelivered copy for "
                        f"`{edit.recipient}` at the fork point. Undelivered: {pending or 'none'}."
                    )


def _is(mail: MailCheckpoint, edit: ReplaceDelivery) -> bool:
    return mail.recipient == edit.recipient and plain_id(mail.send_event_id) == edit.send_event_id


def plain_id(event_id: str) -> str:
    """An event id without the run a cross-run reference names."""
    return event_id.rsplit(":", 1)[-1]


def edited_contexts(
    contexts: Mapping[str, tuple[ChatMessage, ...]], edits: Sequence[Edit]
) -> dict[str, tuple[ChatMessage, ...]]:
    """Each agent whose context an edit changes, with its new context. Indexes refer to the
    context at the fork point: every replacement applies first, then every deletion, so a
    deleted message stays deleted whatever replaced it."""
    changed: dict[str, list[ChatMessage]] = {}
    deleted: dict[str, set[int]] = {}
    for edit in edits:
        match edit:
            case ReplaceMessage():
                current = changed.setdefault(edit.agent_id, list(contexts[edit.agent_id]))
                current[edit.index] = _with_content(current[edit.index], edit.content)
            case DeleteMessage():
                changed.setdefault(edit.agent_id, list(contexts[edit.agent_id]))
                deleted.setdefault(edit.agent_id, set()).add(edit.index)
            case ReplaceDelivery():
                pass
    return {
        agent: tuple(m for i, m in enumerate(msgs) if i not in deleted.get(agent, set()))
        for agent, msgs in changed.items()
    }


def _with_content(message: ChatMessage, content: str) -> ChatMessage:
    match message:
        case SystemMessage() | UserMessage() | ToolMessage() | AssistantMessage():
            return message.model_copy(update={"content": content})


def edit_intervention(
    agent_id: str, before_sha256: str, after: tuple[ChatMessage, ...], parent_id: str
) -> EventDraft:
    """Like a compaction: the agent's new generation, in full."""
    record = InterventionRecord(
        hook="fork",
        action="edit_context",
        target_event_id=None,
        before_sha256=before_sha256,
        after=[m.model_dump(mode="json") for m in after],
    )
    return EventDraft(record=record, agent_id=agent_id, parent_id=parent_id)


def delivery_intervention(
    mail: MailCheckpoint, edit: ReplaceDelivery, before_sha256: str, parent_id: str
) -> EventDraft:
    """Like a `before_deliver` rewrite of the send, for that recipient. `target_event_id` is
    the send's plain id, as the source recorded it."""
    record = InterventionRecord(
        hook="fork",
        action="deliver",
        target_event_id=plain_id(mail.send_event_id),
        before_sha256=before_sha256,
        after={"recipient": mail.recipient, "kind": "deliver", "content": edit.content},
    )
    return EventDraft(record=record, agent_id=mail.recipient, parent_id=parent_id)


def replace_mail(mail: MailCheckpoint, edit: ReplaceDelivery) -> bool:
    return _is(mail, edit)
