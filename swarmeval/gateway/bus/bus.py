"""Channels, the `send_message` tool, and delivery into recipients' contexts.

The bus lives in the run's worker and has no state of its own beyond what the run has
committed. A send commits as a `msg.send` event; the loop then routes it to each recipient
through `before_deliver`, whose verdict (deliver, possibly rewritten; delay; drop) commits with
its interventions. A routed message is pending for its recipient until a `msg.deliver` event for
it commits. The store keeps the same state in `runs.deliveries`.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from swarmeval.runtime.messages import UserMessage
from swarmeval.runtime.records import (
    CommittedEvent,
    EventDraft,
    MailCheckpoint,
    MessageDeliverRecord,
    MessageSendRecord,
)
from swarmeval.runtime.tools import RuntimeOutcome, RuntimeTool


@dataclass(frozen=True)
class ChannelSpec:
    id: str
    members: tuple[str, ...]


class SendArgs(BaseModel):
    channel: str = Field(description="Id of a channel you are a member of.")
    content: str = Field(description="The message. Every other member of the channel gets it.")


@dataclass(frozen=True)
class Delivery:
    message: UserMessage
    """What enters the recipient's context."""
    draft: EventDraft
    """The `msg.deliver` event, committed with the message."""


@dataclass(frozen=True)
class Unrouted:
    """A committed send that has not been through `before_deliver` for `recipient` yet."""

    send: CommittedEvent
    record: MessageSendRecord
    recipient: str


@dataclass(frozen=True)
class _Mail:
    send_seq: int
    send_event_id: str
    record: MessageSendRecord
    content: str
    due_turn: int | None
    """`None`: the recipient's next turn."""
    parent_id: str
    due_at: float | None = None
    """Held until then as well, on the bus's clock (`async` only)."""
    """What the delivered content is from: the last `before_deliver` intervention on it, or
    the send."""


def delivered_message(record: MessageDeliverRecord) -> UserMessage:
    """What a delivery puts into the recipient's context."""
    return UserMessage(
        content=f"Message from {record.sender} on channel `{record.channel}`:\n\n{record.content}"
    )


class MessageBus:
    def __init__(
        self, channels: Sequence[ChannelSpec], clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._channels = {c.id: c for c in channels}
        self._unrouted: list[Unrouted] = []
        self._mail: dict[str, list[_Mail]] = {}

    def tool(self) -> RuntimeTool:
        return RuntimeTool(
            name="send_message",
            description=(
                "Send a message to the other members of a channel. They see it at the start of "
                "their next turn."
            ),
            args=SendArgs,
            run=self._send,
        )

    def on_commit(self, committed: Sequence[CommittedEvent]) -> None:
        """`RunWriter` subscriber: a send needs routing once it has committed."""
        for event in committed:
            if isinstance(event.record, MessageSendRecord):
                self._unrouted.extend(
                    Unrouted(event, event.record, recipient)
                    for recipient in event.record.recipients
                )

    def unrouted(self) -> list[Unrouted]:
        """Sends committed since the last call, one entry per recipient, in send order."""
        found, self._unrouted = self._unrouted, []
        return found

    def route(
        self,
        item: Unrouted,
        content: str,
        *,
        due_turn: int | None,
        parent_id: str | None,
        hold_s: float = 0.0,
    ) -> None:
        """Queues `content` for the recipient, at its next turn or at `due_turn`, and not before
        `hold_s` seconds from now. `parent_id` is the last intervention that decided it, if
        any."""
        self._mail.setdefault(item.recipient, []).append(
            _Mail(
                item.send.seq,
                item.send.event_id,
                item.record,
                content,
                due_turn,
                parent_id or item.send.event_id,
                self._clock() + hold_s if hold_s else None,
            )
        )

    def snapshot(self) -> tuple[MailCheckpoint, ...]:
        """Mail routed and not yet delivered, per recipient in send order. Nothing is unrouted
        between turns: a send is routed in the step that made it."""
        assert not self._unrouted, "snapshots are taken between turns, after routing"
        assert all(m.due_at is None for mail in self._mail.values() for m in mail), (
            "mail held by time exists only under `async`, which takes no checkpoints"
        )
        return tuple(
            MailCheckpoint(
                recipient=recipient,
                send_seq=m.send_seq,
                send_event_id=m.send_event_id,
                send=m.record,
                content=m.content,
                due_turn=m.due_turn,
                parent_id=m.parent_id,
            )
            for recipient, mail in self._mail.items()
            for m in mail
        )

    def restore(self, mail: Sequence[MailCheckpoint]) -> None:
        """A fork's carried mail, as its source's `snapshot` left it."""
        for m in mail:
            self._mail.setdefault(m.recipient, []).append(
                _Mail(m.send_seq, m.send_event_id, m.send, m.content, m.due_turn, m.parent_id)
            )

    def has_mail(self, agent_id: str, turn: int) -> bool:
        """Whether a message is due for `agent_id` at its turn `turn`."""
        now = self._clock()
        return any(_due(m, turn, now) for m in self._mail.get(agent_id, ()))

    def next_due_in(self, agent_id: str) -> float | None:
        """Seconds until the next message held by time for `agent_id` comes due; `None` when
        none is."""
        now = self._clock()
        held = [
            m.due_at - now
            for m in self._mail.get(agent_id, ())
            if m.due_at is not None and m.due_turn is None
        ]
        return max(0.0, min(held)) if held else None

    def take(self, agent_id: str, turn: int) -> list[Delivery]:
        """Every message due for `agent_id` at its turn `turn`, in send order; delayed ones not
        yet due stay. The caller commits the drafts with the messages; until then a crash
        leaves them undelivered in the store."""
        mail = self._mail.pop(agent_id, [])
        now = self._clock()
        held = [m for m in mail if not _due(m, turn, now)]
        if held:
            self._mail[agent_id] = held
        deliveries: list[Delivery] = []
        for m in mail:
            if not _due(m, turn, now):
                continue
            record = MessageDeliverRecord(
                channel=m.record.channel,
                sender=m.record.sender,
                recipient=agent_id,
                send_seq=m.send_seq,
                content=m.content,
            )
            deliveries.append(
                Delivery(
                    message=delivered_message(record),
                    draft=EventDraft(record=record, agent_id=agent_id, parent_id=m.parent_id),
                )
            )
        return deliveries

    def post(
        self, channel: str, sender: str, content: str, *, by: str, parent_id: str
    ) -> EventDraft:
        """The `msg.send` of a message extension `by` put on `channel` as if `sender` sent it.
        Every member but `sender` gets it."""
        members = self._channels[channel].members
        record = MessageSendRecord(
            channel=channel,
            sender=sender,
            content=content,
            recipients=tuple(m for m in members if m != sender),
            call_id=None,
        )
        return EventDraft(record=record, extension=by, parent_id=parent_id)

    def _send(self, args: SendArgs, sender: str, call_id: str) -> RuntimeOutcome:
        channel = self._channels.get(args.channel)
        if channel is None or sender not in channel.members:
            mine = sorted(c.id for c in self._channels.values() if sender in c.members)
            return RuntimeOutcome(
                content=f"You cannot send on channel `{args.channel}`. "
                f"Your channels: {', '.join(mine) or 'none'}.",
                is_error=True,
            )
        recipients = tuple(m for m in channel.members if m != sender)
        record = MessageSendRecord(
            channel=channel.id,
            sender=sender,
            content=args.content,
            recipients=recipients,
            call_id=call_id,
        )
        return RuntimeOutcome(
            content=f"Sent to {', '.join(recipients)}.",
            events=(EventDraft(record=record, agent_id=sender),),
        )


def _due(mail: _Mail, turn: int, now: float) -> bool:
    return (mail.due_turn is None or mail.due_turn <= turn) and (
        mail.due_at is None or mail.due_at <= now
    )
