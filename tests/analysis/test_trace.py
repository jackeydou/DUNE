from typing import Any

import pyarrow as pa
import pytest

from swarmeval.analysis.trace import TraceError, text, trace
from swarmeval.events import EVENTS_SCHEMA
from tests.analysis.test_timeline import event


def linked(seq: int, parent: int | None, agent: str | None, payload: dict[str, Any]) -> Any:
    row = event(seq, agent, payload)
    row["parent_id"] = None if parent is None else f"e{parent}"
    return row


START: dict[str, Any] = {
    "event": "info",
    "source": "swarmeval.lifecycle",
    "data": {"status": "started"},
}
SEND: dict[str, Any] = {
    "event": "info",
    "source": "swarmeval.msg.send",
    "data": {"content": "raise\nprices"},
}
DELIVER: dict[str, Any] = {
    "event": "info",
    "source": "swarmeval.msg.deliver",
    "data": {"content": "raise"},
}
CALL: dict[str, Any] = {
    "event": "tool",
    "function": "send_message",
    "arguments": {},
    "result": "Sent.",
}

RUN = pa.Table.from_pylist(
    [
        linked(1, None, None, START),
        linked(2, 1, "a", {"event": "model", "output": {"choices": []}}),
        linked(3, 2, "a", CALL),
        linked(4, 2, "a", SEND),
        linked(5, 1, "b", {"event": "model", "output": {"choices": []}}),
        linked(6, 4, "b", DELIVER),
    ],
    schema=EVENTS_SCHEMA,
)


def test_the_chain_runs_from_the_root_to_the_event() -> None:
    result = trace(RUN, "e6")

    assert [link.seq for link in result.links] == [1, 2, 4, 6]
    assert text(result).splitlines()[4:] == [
        '[e1] #1 - swarmeval.lifecycle: {"status": "started"}',
        "[e2] #2 a model: (no output)",
        '[e4] #4 a swarmeval.msg.send: {"content": "raise\\\\nprices"}',
        '[e6] #6 b swarmeval.msg.deliver: {"content": "raise"}',
    ]


def test_the_root_traces_to_itself() -> None:
    assert [link.event_id for link in trace(RUN, "e1").links] == ["e1"]


def test_an_unknown_event_is_refused() -> None:
    with pytest.raises(TraceError, match="event e9 is not in run run_t"):
        trace(RUN, "e9")


def test_a_parent_missing_from_the_run_is_refused() -> None:
    broken = pa.Table.from_pylist(
        [linked(1, None, None, START), linked(2, 7, "a", CALL)], schema=EVENTS_SCHEMA
    )

    with pytest.raises(TraceError, match=r"event e2 \(seq 2\) names parent e7"):
        trace(broken, "e2")


def test_a_loop_is_refused() -> None:
    looped = pa.Table.from_pylist(
        [linked(1, 2, None, START), linked(2, 1, "a", CALL)], schema=EVENTS_SCHEMA
    )

    with pytest.raises(TraceError, match="loops at event e2"):
        trace(looped, "e2")


def test_long_events_are_cut() -> None:
    long = pa.Table.from_pylist(
        [linked(1, None, None, {"event": "info", "source": "x", "data": "y" * 50})],
        schema=EVENTS_SCHEMA,
    )

    (link,) = trace(long, "e1", event_chars=10).links
    assert link.text == 'x: "yyyyyy …[cut: 55 characters]'
