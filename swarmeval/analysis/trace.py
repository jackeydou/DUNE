"""The `trace` job: one event's causal chain, from the run's root down to it, one line per event
(docs/services/analysis.md#capabilities).

Read from the run's `events.parquet`, following each event's `parent_id`
(docs/event-log.md#causal-parents). A fork's events reach into its source through parents
written `run_id:event_id`; the chain goes on in that run's export. Lines are the same text the
timeline and the judge show.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
from pydantic import JsonValue

from swarmeval.events.render import event_line

COMPLETE_PARENTS_SCHEMA = 5
"""From this event schema version on, only a run's first event has no parent; older runs end
their chains early (docs/event-log.md#causal-parents)."""


class TraceError(Exception):
    """The event is not in the run, or its chain is broken."""


class EventNotInRun(TraceError):
    """The event to trace is not one of the run's."""


@dataclass(frozen=True)
class Link:
    run_id: str
    seq: int
    event_id: str
    agent_id: str | None
    type: str
    text: str


@dataclass(frozen=True)
class Trace:
    run_id: str
    event_id: str
    links: tuple[Link, ...]
    """The run's root first, the traced event last."""


def _rows(table: pa.Table) -> dict[str, dict[str, Any]]:
    return {
        str(row["event_id"]): row
        for row in table.select(
            ["run_id", "seq", "event_id", "agent_id", "type", "parent_id", "payload"]
        ).to_pylist()
    }


def trace(
    table: pa.Table,
    event_id: str,
    *,
    event_chars: int = 400,
    load: Callable[[str], pa.Table] | None = None,
) -> Trace:
    """`table` is a run's `events.parquet`; `load` reads another run's, for a fork's parents in
    its source. Raises `TraceError` when `event_id` is not in it, or when a parent is missing
    from its run, the chain loops, or a schema 5 event other than its run's first has no parent,
    none of which the run's writer produces."""
    rows = _rows(table)
    traced_run = _run_of(rows)
    if event_id not in rows:
        run_ids = {row["run_id"] for row in rows.values()}
        raise EventNotInRun(
            f"event {event_id} is not in run {', '.join(sorted(run_ids)) or '(empty)'}. "
            "Copy the id from the run's timeline."
        )
    first_seq = min(row["seq"] for row in rows.values())
    chain: list[Link] = []
    seen: set[str] = set()
    current: str | None = event_id
    while current is not None:
        if ":" in current:
            other, current = current.rsplit(":", 1)
            if load is None:
                raise TraceError(
                    f"event {chain[-1].event_id} names parent {current} of run {other}; pass a "
                    "loader to follow a fork into its source."
                )
            rows = _rows(load(other))
            first_seq = min(row["seq"] for row in rows.values())
        row = rows.get(current)
        if row is None:
            child = chain[-1]
            raise TraceError(
                f"event {child.event_id} (seq {child.seq}) names parent {current}, which is not "
                f"in run {_run_of(rows)}. The export is incomplete or was changed."
            )
        if current in seen:
            raise TraceError(
                f"the parent chain of event {event_id} loops at event {current} (seq "
                f"{row['seq']}). The export was changed after the run wrote it."
            )
        seen.add(current)
        payload = json.loads(row["payload"])
        chain.append(
            Link(
                run_id=_run_of(rows),
                seq=row["seq"],
                event_id=current,
                agent_id=row["agent_id"],
                type=row["type"],
                text=event_line(payload, event_chars),
            )
        )
        current = row["parent_id"]
        if (
            current is None
            and row["seq"] != first_seq
            and _schema(payload) >= COMPLETE_PARENTS_SCHEMA
        ):
            raise TraceError(
                f"event {row['event_id']} (seq {row['seq']}) of run {_run_of(rows)} has no parent, "
                f"but under event schema {_schema(payload)} only the run's first event (seq "
                f"{first_seq}) is a root. The export is incomplete or was changed."
            )
    return Trace(run_id=traced_run, event_id=event_id, links=tuple(reversed(chain)))


def _schema(payload: dict[str, JsonValue]) -> int:
    metadata = payload.get("metadata")
    swarmeval = metadata.get("swarmeval") if isinstance(metadata, dict) else None
    version = swarmeval.get("schema_version") if isinstance(swarmeval, dict) else None
    return version if isinstance(version, int) else 0


def _run_of(rows: dict[str, dict[str, object]]) -> str:
    return str(next(iter(rows.values()))["run_id"])


def text(t: Trace) -> str:
    """One line per event, `[event_id] #seq agent text`, the root first."""
    lines = [
        f"# Run {t.run_id}: how event {t.event_id} came about",
        "",
        f"{len(t.links)} events, the run's root first. `-` is the run itself.",
        "",
    ]
    lines.extend(
        f"[{x.event_id if x.run_id == t.run_id else f'{x.run_id}:{x.event_id}'}] #{x.seq} "
        f"{x.agent_id or '-'} {x.text}"
        for x in t.links
    )
    return "\n".join(lines) + "\n"
