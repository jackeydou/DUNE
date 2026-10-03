import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest

from swarmeval.analysis.exports import ExportError, runs_of
from swarmeval.analysis.timeline import RUN_LANE, markdown, page, timeline
from swarmeval.events import EVENTS_SCHEMA
from tests.analysis.test_report import summary, table

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def event(
    seq: int, agent: str | None, payload: dict[str, Any], *, type_: str = "info"
) -> dict[str, Any]:
    return {
        "run_id": "run_t",
        "seq": seq,
        "event_id": f"e{seq}",
        "ts": T0 + timedelta(seconds=seq * 1.5),
        "type": type_,
        "source": "orchestrator",
        "agent_id": agent,
        "sandbox_id": None,
        "parent_id": None,
        "prev_hash": "00",
        "hash": "11",
        "payload": json.dumps(payload),
    }


EVENTS = pa.Table.from_pylist(
    [
        event(1, None, {"event": "info", "source": "swarmeval.lifecycle", "data": {"s": 1}}),
        event(
            2,
            "qa",
            {"event": "tool", "function": "shell", "arguments": {"cmd": "ls | wc"}, "result": "3"},
            type_="tool",
        ),
        event(
            3,
            "dev",
            {"event": "tool", "function": "shell", "arguments": {}, "result": "a\n<script>"},
            type_="tool",
        ),
        event(
            4, None, {"event": "score", "scorer": "tampered", "score": {"value": 1}}, type_="score"
        ),
    ],
    schema=EVENTS_SCHEMA,
)


def test_every_event_lands_in_its_lane_in_seq_order() -> None:
    result = timeline(EVENTS)

    assert result.lanes == (RUN_LANE, "qa", "dev")
    assert [(e.seq, e.lane, e.seconds) for e in result.entries] == [
        (1, RUN_LANE, 0.0),
        (2, "qa", 1.5),
        (3, "dev", 3.0),
        (4, RUN_LANE, 4.5),
    ]
    text = markdown(result)
    lines = text.splitlines()
    assert lines[0] == "# Run run_t"
    assert lines[4] == "| seq | time | event | - | qa | dev |"
    assert lines[7] == '| 2 | 1.50 | `e2` |  | tool shell({"cmd": "ls \\| wc"}) -> 3 |  |'
    assert lines[8] == "| 3 | 3.00 | `e3` |  |  | tool shell({}) -> a\\n<script> |"
    assert lines[9] == "| 4 | 4.50 | `e4` | score tampered = 1: None |  |  |"


def test_lanes_and_a_seq_range_narrow_it_but_times_stay_run_relative() -> None:
    result = timeline(EVENTS, agents=["dev", RUN_LANE], from_seq=2)

    assert [(e.seq, e.seconds) for e in result.entries] == [(3, 3.0), (4, 4.5)]
    assert result.lanes == (RUN_LANE, "dev")
    assert "2 of 4 events, seq 3 to 4" in markdown(result)
    assert "No events match" in markdown(timeline(EVENTS, agents=["nobody"]))


def test_long_events_are_cut_and_marked() -> None:
    (entry,) = timeline(EVENTS, from_seq=3, to_seq=3, event_chars=10).entries

    assert entry.text == "tool shell …[cut: 28 characters]"


def test_the_page_escapes_run_content_and_loads_nothing() -> None:
    html = page(timeline(EVENTS))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "http" not in html.replace('name="viewport"', "")
    assert html.count("<tr class=") == 4


def test_runs_of_a_submission_are_picked_by_status() -> None:
    summaries = table(
        summary("r1"), summary("r2", status="cancelled"), summary("r3", status="failed")
    )

    assert runs_of(summaries, ["sub1"], ["done", "cancelled"]) == ["r1", "r2"]
    with pytest.raises(ExportError, match="no runs that ended `done`"):
        runs_of(summaries, ["sub2"], ["done"])
