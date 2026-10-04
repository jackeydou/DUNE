"""A hook's change as an `intervention` event: what it changed (by hash) and what it made of it
(docs/agent-runtime.md#hooks)."""

import hashlib
from collections.abc import Sequence

from pydantic import BaseModel, JsonValue
from pydantic_core import to_json

from swarmeval.runtime.extensions.api import Envelope
from swarmeval.runtime.records import EventDraft, HookName, InterventionRecord


def sha256_json(value: JsonValue) -> str:
    return hashlib.sha256(to_json(value)).hexdigest()


def _dump(value: BaseModel | Sequence[BaseModel]) -> JsonValue:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return [item.model_dump(mode="json") for item in value]


def intervention(
    instance_id: str,
    hook: HookName,
    action: str,
    target_event_id: str | None,
    trigger_id: str,
    before: BaseModel | Sequence[BaseModel],
    after: BaseModel | Sequence[BaseModel],
) -> EventDraft:
    """Parented to the hook's trigger, which is the target itself when there is one."""
    record = InterventionRecord(
        hook=hook,
        action=action,
        target_event_id=target_event_id,
        before_sha256=sha256_json(_dump(before)),
        after=_dump(after),
    )
    return EventDraft(record=record, extension=instance_id, parent_id=trigger_id)


def delivery_intervention(instance_id: str, envelope: Envelope, decision: BaseModel) -> EventDraft:
    record = InterventionRecord(
        hook="before_deliver",
        action=str(decision.model_dump()["kind"]),
        target_event_id=envelope.send_event_id,
        before_sha256=sha256_json(envelope.content),
        after={
            "recipient": envelope.recipient,
            **decision.model_dump(mode="json", exclude_none=True),
        },
    )
    return EventDraft(
        record=record,
        agent_id=envelope.recipient,
        extension=instance_id,
        parent_id=envelope.send_event_id,
    )
