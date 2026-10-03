"""Channels, the `send_message` tool, and delivery into recipients' contexts.

The bus lives in the run's worker and has no state of its own beyond what the run has
committed: a send is pending for a recipient from the moment its `msg.send` event commits until
a `msg.deliver` event for that recipient commits. The store keeps the same state in
`runs.deliveries`.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from swarmeval.runtime.messages import UserMessage
from swarmeval.runtime.records import (
    CommittedEvent,
    EventDraft,
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


def delivered_message(record: MessageDeliverRecord) -> UserMessage:
    """What a delivery puts into the recipient's context."""
    return UserMessage(
        content=f"Message from {record.sender} on channel `{record.channel}`:\n\n{record.content}"
    )


class MessageBus:
    def __init__(self, channels: Sequence[ChannelSpec]) -> None:
        self._channels = {c.id: c for c in channels}
        self._pending: dict[str, list[CommittedEvent]] = {}

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
        """`RunWriter` subscriber: a send becomes pending once it has committed."""
        for event in committed:
            if isinstance(event.record, MessageSendRecord):
                for recipient in event.record.recipients:
                    self._pending.setdefault(recipient, []).append(event)

    def has_mail(self, agent_id: str) -> bool:
        return bool(self._pending.get(agent_id))

    def take(self, agent_id: str) -> list[Delivery]:
        """Every pending message for `agent_id`, in send order. The caller commits the drafts
        with the messages; until then a crash leaves them pending in the store."""
        deliveries: list[Delivery] = []
        for event in self._pending.pop(agent_id, []):
            send = event.record
            assert isinstance(send, MessageSendRecord)
            record = MessageDeliverRecord(
                channel=send.channel,
                sender=send.sender,
                recipient=agent_id,
                send_seq=event.seq,
                content=send.content,
            )
            deliveries.append(
                Delivery(
                    message=delivered_message(record),
                    draft=EventDraft(record=record, agent_id=agent_id, parent_id=event.event_id),
                )
            )
        return deliveries

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
