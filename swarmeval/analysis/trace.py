"""The `trace` job: one event's causal chain, from the run's root down to it, one line per event
(docs/services/analysis.md#capabilities).

Read from the run's `events.parquet`, following each event's `parent_id`
(docs/event-log.md#causal-parents). Lines are the same text the timeline and the judge show.
"""

import json
from dataclasses import dataclass

import pyarrow as pa

from swarmeval.analysis.render import event_line


class TraceError(Exception):
    """The event is not in the run, or its chain is broken."""


@dataclass(frozen=True)
class Link:
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


def trace(table: pa.Table, event_id: str, *, event_chars: int = 400) -> Trace:
    """`table` is a run's `events.parquet`. Raises `TraceError` when `event_id` is not in it, or
    when a parent is missing from the run or the chain loops, which the run's writer never
    produces."""
    rows = {
        row["event_id"]: row
        for row in table.select(
            ["run_id", "seq", "event_id", "agent_id", "type", "parent_id", "payload"]
        ).to_pylist()
    }
    if event_id not in rows:
        run_ids = {row["run_id"] for row in rows.values()}
        raise TraceError(
            f"event {event_id} is not in run {', '.join(sorted(run_ids)) or '(empty)'}. "
            "Copy the id from the run's timeline."
        )
    chain: list[Link] = []
    seen: set[str] = set()
    current: str | None = event_id
    while current is not None:
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
        chain.append(
            Link(
                seq=row["seq"],
                event_id=current,
                agent_id=row["agent_id"],
                type=row["type"],
                text=event_line(json.loads(row["payload"]), event_chars),
            )
        )
        current = row["parent_id"]
    return Trace(run_id=_run_of(rows), event_id=event_id, links=tuple(reversed(chain)))


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
    lines.extend(f"[{x.event_id}] #{x.seq} {x.agent_id or '-'} {x.text}" for x in t.links)
    return "\n".join(lines) + "\n"
