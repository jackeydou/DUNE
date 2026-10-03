"""The `timeline` job: one run's events in `seq` order, one lane per agent, as Markdown and
optionally as a self-contained HTML page (docs/services/analysis.md#capabilities).

Read from the run's `events.parquet`. Every event is shown, scores included, each as one line
(`render.event_line`), so the ids in it are the ones judge verdicts cite.
"""

import html
import json
from collections.abc import Sequence
from dataclasses import dataclass

import duckdb
import pyarrow as pa

from swarmeval.analysis.render import event_line

RUN_LANE = "-"
"""The lane of events no agent caused: lifecycle, scores, extensions' own events."""


@dataclass(frozen=True)
class Entry:
    seq: int
    event_id: str
    seconds: float
    """Since the run's first event, whatever the filter."""
    lane: str
    type: str
    text: str


@dataclass(frozen=True)
class Timeline:
    run_id: str
    events: int
    """In the run, before filtering."""
    lanes: tuple[str, ...]
    """`RUN_LANE` first when it has events, then agents in the order they first appear."""
    entries: tuple[Entry, ...]


def timeline(
    table: pa.Table,
    *,
    agents: Sequence[str] = (),
    from_seq: int | None = None,
    to_seq: int | None = None,
    event_chars: int = 400,
) -> Timeline:
    """`table` is a run's `events.parquet`. `agents` keeps those lanes only (`RUN_LANE` for
    events no agent caused); empty keeps every lane. `from_seq` / `to_seq` are inclusive."""
    con = duckdb.connect()
    con.register("events", table)
    first = con.execute("SELECT count(*), any_value(run_id) FROM events").fetchone()
    assert first is not None, "an aggregate returns one row"
    count, run_id = first
    if not count:
        raise ValueError("the events table is empty; a run's export always holds its events.")
    rows = con.execute(
        """
        SELECT seq, event_id, epoch(ts) - (SELECT epoch(min(ts)) FROM events),
               coalesce(agent_id, $run_lane), type, payload
        FROM events
        WHERE ($from_seq IS NULL OR seq >= $from_seq)
          AND ($to_seq IS NULL OR seq <= $to_seq)
          AND ($all OR list_contains($agents, coalesce(agent_id, $run_lane)))
        ORDER BY seq
        """,
        {
            "run_lane": RUN_LANE,
            "from_seq": from_seq,
            "to_seq": to_seq,
            "all": not agents,
            "agents": list(agents) or [""],
        },
    ).fetchall()
    entries = tuple(
        Entry(
            seq=seq,
            event_id=event_id,
            seconds=seconds,
            lane=lane,
            type=type_,
            text=event_line(json.loads(payload), event_chars),
        )
        for seq, event_id, seconds, lane, type_, payload in rows
    )
    seen = list(dict.fromkeys(e.lane for e in entries))
    lanes = ([RUN_LANE] if RUN_LANE in seen else []) + [lane for lane in seen if lane != RUN_LANE]
    return Timeline(run_id=run_id, events=count, lanes=tuple(lanes), entries=entries)


def _summary(t: Timeline) -> str:
    if not t.entries:
        return f"No events match, of the run's {t.events}."
    span = f"seq {t.entries[0].seq} to {t.entries[-1].seq}"
    return (
        f"{len(t.entries)} of {t.events} events, {span}. Lanes: {', '.join(t.lanes)} "
        f"(`{RUN_LANE}` is the run itself). Time is seconds since the run's first event."
    )


def markdown(t: Timeline) -> str:
    """A table with a column per lane; each event fills its own lane's cell."""
    lines = [f"# Run {t.run_id}", "", _summary(t), ""]
    if not t.entries:
        return "\n".join(lines)
    lines.append("| seq | time | event | " + " | ".join(t.lanes) + " |")
    lines.append("|---:|---:|---|" + "---|" * len(t.lanes))
    for e in t.entries:
        cells = [_cell(e.text) if lane == e.lane else "" for lane in t.lanes]
        lines.append(f"| {e.seq} | {e.seconds:.2f} | `{e.event_id}` | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    """Event text is one line already; a pipe would end the cell."""
    return text.replace("|", "\\|")


def _lane_class(lane: str) -> str:
    return " class=run" if lane == RUN_LANE else ""


def page(t: Timeline) -> str:
    """The same table as a standalone HTML page: no scripts, no external resources."""
    head = "".join(f"<th>{html.escape(lane)}</th>" for lane in t.lanes)
    body = "\n".join(
        f'<tr class="t-{html.escape(e.type.replace(".", "-"))}">'
        f"<td class=n>{e.seq}</td><td class=n>{e.seconds:.2f}</td>"
        f"<td><code>{html.escape(e.event_id)}</code></td>"
        + "".join(
            f"<td{_lane_class(lane)}>{html.escape(e.text) if lane == e.lane else ''}</td>"
            for lane in t.lanes
        )
        + "</tr>"
        for e in t.entries
    )
    title = html.escape(f"Run {t.run_id}")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; --line: #8884; --run: #8881; }}
body {{ font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; margin: 16px; }}
table {{ border-collapse: collapse; width: 100%; table-layout: fixed; }}
th, td {{ border: 1px solid var(--line); padding: 4px 6px; vertical-align: top;
  overflow-wrap: anywhere; }}
th {{ position: sticky; top: 0; background: Canvas; text-align: left; }}
td.n {{ text-align: right; width: 4em; }}
td:nth-child(3) {{ width: 13em; }}
td.run {{ background: var(--run); }}
</style></head>
<body><h1>{title}</h1><p>{html.escape(_summary(t))}</p>
<table><thead><tr><th>seq</th><th>time</th><th>event</th>{head}</tr></thead>
<tbody>
{body}
</tbody></table></body></html>
"""
