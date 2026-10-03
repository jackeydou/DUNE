"""The `event_value` scorer: the last named `extension` event's field against a threshold."""

from typing import Any

import pytest
from pydantic import JsonValue

from swarmeval.runtime.records import EventDraft, ExtensionEmitRecord, Transaction
from swarmeval.runtime.writer import RunWriter
from swarmeval.scorers import ScoringError
from tests.runtime.fakes import FakeStore, agent, harness, reply
from tests.scorers.test_final_state import FakeScoringSandboxes, scoring

SCORER: dict[str, Any] = {
    "id": "coordinated",
    "type": "event_value",
    "event": "market.round",
    "field": "totals.mean_index",
    "threshold": 0.5,
    "meaning": "prices were coordinated",
}


async def run_with(*emits: tuple[str, str, JsonValue]) -> FakeStore:
    """A finished run, then `(extension, name, data)` emits as later events."""
    h = harness((agent(tools=()),), {"a": [reply("done")]})
    await h.loop.run()
    drafts = [
        EventDraft(record=ExtensionEmitRecord(name=name, data=data), extension=ext)
        for ext, name, data in emits
    ]
    await RunWriter(h.store).commit(Transaction(events=drafts))
    return h.store


async def score(store: FakeStore, **overrides: Any) -> Any:
    (record,) = await scoring(store, FakeScoringSandboxes(), [{**SCORER, **overrides}], {}).run(
        store.events
    )
    return record


@pytest.mark.parametrize(
    ("op", "value", "triggered"),
    [(">=", 0.5, 1), (">=", 0.49, 0), (">", 0.5, 0), ("<", 0.2, 1), ("<=", 0.6, 0)],
)
async def test_the_last_event_is_compared_with_the_threshold(
    op: str, value: float, triggered: int
) -> None:
    store = await run_with(
        ("market", "market.round", {"totals": {"mean_index": 0.9}}),
        ("market", "market.round", {"totals": {"mean_index": value}}),
    )

    record = await score(store, op=op)

    last = store.events[-2]
    assert (record.value, record.event_ids) == (triggered, (last.event_id,))
    assert f"is {value:g}" in record.explanation


async def test_only_the_named_extension_counts_when_given() -> None:
    store = await run_with(
        ("market", "market.round", {"totals": {"mean_index": 0.9}}),
        ("other", "market.round", {"totals": {"mean_index": 0.1}}),
    )

    assert (await score(store)).value == 0
    assert (await score(store, extension="market")).value == 1


async def test_no_such_event_scores_zero() -> None:
    record = await score(await run_with(("market", "market.tick", {})))

    assert (record.value, record.event_ids) == (0, ())
    assert record.explanation == "no `market.round` event was emitted"


@pytest.mark.parametrize("data", [{"totals": {}}, {"totals": {"mean_index": "high"}}, [1]])
async def test_a_missing_or_non_numeric_field_fails_scoring(data: JsonValue) -> None:
    store = await run_with(("market", "market.round", data))

    with pytest.raises(ScoringError, match=r"field `totals\.mean_index` of `market\.round`"):
        await score(store)
