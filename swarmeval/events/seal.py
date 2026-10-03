"""Sealing: event drafts become chained `runs.events` rows. Pure; the store writes the rows."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import rfc8785
from pydantic import JsonValue

from swarmeval.events.chain import genesis, link
from swarmeval.events.convert import Attribution, event_type, source_of, to_event
from swarmeval.runtime.records import (
    CommittedEvent,
    EventDraft,
    IsolationProbeRecord,
    SandboxExecRecord,
    ToolCallRecord,
)


@dataclass(frozen=True)
class ChainHead:
    seq: int
    hash: bytes
    started_at: datetime | None
    """Timestamp of seq 1, the zero point of Inspect's `working_start`."""

    @staticmethod
    def start(run_id: str) -> "ChainHead":
        return ChainHead(seq=0, hash=genesis(run_id), started_at=None)


def seal(
    drafts: Sequence[EventDraft],
    head: ChainHead,
    *,
    run_id: str,
    workspace: str,
    sandboxes: Mapping[str, str | None],
) -> tuple[list[CommittedEvent], list[dict[str, object]], ChainHead]:
    """Returns the committed events, one `runs.events` row per draft, and the new head.
    `sandboxes` maps each agent to its sandbox, to attribute tool events."""
    committed: list[CommittedEvent] = []
    rows: list[dict[str, object]] = []
    for draft in drafts:
        seq = head.seq + 1
        sandbox_id = _sandbox_of(draft, sandboxes)
        event = to_event(
            draft.record,
            Attribution(
                workspace=workspace,
                seq=seq,
                parent_id=draft.parent_id,
                agent_id=draft.agent_id,
                sandbox_id=sandbox_id,
                extension=draft.extension,
            ),
        )
        started_at = head.started_at or event.timestamp
        event.working_start = (event.timestamp - started_at).total_seconds()
        payload: JsonValue = event.model_dump(mode="json", exclude_none=True)
        try:
            digest = link(head.hash, seq, payload)
        except rfc8785.CanonicalizationError as err:
            raise ValueError(
                f"run {run_id}: the {draft.record.kind} event at seq {seq} (agent "
                f"{draft.agent_id}, extension {draft.extension}) carries a value JSON cannot "
                f"represent exactly: {err}. The component that produced it must send finite "
                "floats and integers within ±(2**53 - 1)."
            ) from err
        assert event.uuid is not None, "Inspect assigns a uuid at construction"
        rows.append(
            {
                "run_id": run_id,
                "seq": seq,
                "event_id": event.uuid,
                "ts": event.timestamp,
                "type": event_type(event),
                "source": source_of(draft.record),
                "agent_id": draft.agent_id,
                "sandbox_id": sandbox_id,
                "parent_id": draft.parent_id,
                "prev_hash": head.hash,
                "hash": digest,
                "payload": payload,
            }
        )
        committed.append(
            CommittedEvent(
                event_id=event.uuid,
                seq=seq,
                agent_id=draft.agent_id,
                extension=draft.extension,
                parent_id=draft.parent_id,
                record=draft.record,
            )
        )
        head = ChainHead(seq=seq, hash=digest, started_at=started_at)
    return committed, rows, head


def _sandbox_of(draft: EventDraft, sandboxes: Mapping[str, str | None]) -> str | None:
    match draft.record:
        case SandboxExecRecord(sandbox_id=sandbox_id) | IsolationProbeRecord(sandbox_id=sandbox_id):
            return sandbox_id
        case ToolCallRecord(exec_result=result) if result is not None:
            assert draft.agent_id is not None, "the loop attributes every tool call"
            return sandboxes[draft.agent_id]
        case _:
            return None
