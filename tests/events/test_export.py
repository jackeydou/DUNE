"""Export without a database: a run on the in-memory store, sealed into the rows Postgres holds."""

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from inspect_ai.event import ModelEvent, SpanBeginEvent, SpanEndEvent
from inspect_ai.log import EvalLog, read_eval_log
from inspect_ai.scorer import Score
from pydantic import JsonValue

from swarmeval.events import (
    ChainError,
    ChainRow,
    RunHeader,
    StoredRun,
    assemble,
    verify,
    write_eval,
)
from swarmeval.events.seal import ChainHead, seal
from swarmeval.runtime import Limits
from swarmeval.runtime.messages import ChatMessage
from swarmeval.runtime.records import EventDraft, LifecycleRecord, ScoreRecord, Transaction
from tests.runtime.fakes import NO_LIMITS, FakeStore, agent, call, harness, reply

RUN = "run_x"
HEADER = RunHeader(
    run_id=RUN,
    case_id="demo",
    variant=0,
    task_args={"model": "test-model"},
    epoch=1,
    epochs=3,
    input="Do the task.",
    models={"a": "test-model", "b": "test-model"},
)


def seal_all(drafts: list[EventDraft]) -> list[ChainRow]:
    _, rows, _ = seal(
        drafts,
        ChainHead.start(RUN),
        run_id=RUN,
        workspace="ws",
        sandboxes={"a": "box_a", "b": "box_b"},
    )
    return [
        ChainRow(
            seq=cast(int, r["seq"]),
            prev_hash=cast(bytes, r["prev_hash"]),
            hash=cast(bytes, r["hash"]),
            payload=cast(JsonValue, r["payload"]),
        )
        for r in rows
    ]


def stored(fake: FakeStore) -> StoredRun:
    drafts = [
        EventDraft(
            record=e.record, agent_id=e.agent_id, extension=e.extension, parent_id=e.parent_id
        )
        for e in fake.events
    ]
    by_gen: dict[tuple[str, int], list[ChatMessage]] = {
        (agent_id, gen): messages
        for agent_id, gens in fake.generations.items()
        for gen, messages in enumerate(gens)
    }
    return StoredRun(workspace="ws", events=seal_all(drafts), messages=by_gen)


async def two_agent_run(limits: Limits = NO_LIMITS) -> FakeStore:
    h = harness(
        (agent("a"), agent("b")),
        {
            "a": [reply("", call("shell", '{"cmd": "ls"}')), reply("done")],
            "b": [reply("nothing to do")],
        },
        limits=limits,
    )
    await h.loop.run()
    return h.store


def read_back(log: EvalLog, tmp_path: Path) -> EvalLog:
    path = tmp_path / "sample.eval"
    write_eval(log, path)
    return read_eval_log(path)


async def test_a_run_exports_to_an_eval_log_inspect_reads_back(tmp_path: Path) -> None:
    run = stored(await two_agent_run())

    log = read_back(assemble(HEADER, run), tmp_path)

    assert log.status == "success"
    assert (log.eval.task, log.eval.model, log.eval.task_args) == (
        "demo",
        "test-model",
        {"model": "test-model"},
    )
    assert log.eval.config.epochs == 3
    assert "inspect_ai" in log.eval.packages
    assert log.samples is not None
    (sample,) = log.samples
    assert (sample.id, sample.epoch, sample.input, sample.uuid) == ("demo", 1, "Do the task.", RUN)
    assert sample.model_usage["test-model"].total_tokens == 30
    assert sample.limit is None


async def test_each_agent_is_a_span_holding_its_events(tmp_path: Path) -> None:
    log = read_back(assemble(HEADER, stored(await two_agent_run())), tmp_path)

    assert log.samples is not None
    sample_events = log.samples[0].events
    begins = [e.id for e in sample_events if isinstance(e, SpanBeginEvent)]
    ends = [e.id for e in sample_events if isinstance(e, SpanEndEvent)]
    assert begins == ["agent:a", "agent:b"]
    assert sorted(ends) == ["agent:a", "agent:b"]
    for event in sample_events:
        if isinstance(event, SpanBeginEvent | SpanEndEvent):
            continue
        assert event.metadata is not None
        agent_id = event.metadata["swarmeval"]["agent_id"]
        assert event.span_id == (None if agent_id is None else f"agent:{agent_id}")


async def test_model_input_is_expanded_from_the_stored_context(tmp_path: Path) -> None:
    log = read_back(assemble(HEADER, stored(await two_agent_run())), tmp_path)

    assert log.samples is not None
    inputs = [
        [m.role for m in e.input]
        for e in log.samples[0].events
        if isinstance(e, ModelEvent) and e.span_id == "agent:a"
    ]
    assert inputs == [["system", "user"], ["system", "user", "assistant", "tool"]]


async def test_exported_events_carry_the_chain(tmp_path: Path) -> None:
    run = stored(await two_agent_run())

    log = read_back(assemble(HEADER, run), tmp_path)

    assert log.samples is not None
    chained = [
        e.metadata["swarmeval"]
        for e in log.samples[0].events
        if e.metadata is not None and "hash" in e.metadata["swarmeval"]
    ]
    assert [c["hash"] for c in chained] == [r.hash.hex() for r in run.events]
    assert [c["prev_hash"] for c in chained] == [r.prev_hash.hex() for r in run.events]
    assert verify(RUN, run.events) == len(chained)


async def test_a_limit_hit_becomes_the_sample_limit(tmp_path: Path) -> None:
    run = stored(await two_agent_run(Limits(max_turns=1)))

    log = read_back(assemble(HEADER, run), tmp_path)

    assert log.samples is not None
    limit = log.samples[0].limit
    assert limit is not None
    assert (limit.type, limit.limit) == ("turn", 1)


def test_a_failed_run_exports_as_an_error(tmp_path: Path) -> None:
    rows = seal_all(
        [
            EventDraft(record=LifecycleRecord(status="started")),
            EventDraft(record=LifecycleRecord(status="failed", error="hook blew up")),
        ]
    )

    log = read_back(assemble(HEADER, StoredRun(workspace="ws", events=rows, messages={})), tmp_path)

    assert log.status == "error"
    assert log.error is not None
    assert log.error.message == "hook blew up"


async def test_rows_that_do_not_verify_are_not_exported() -> None:
    run = stored(await two_agent_run())
    rows = list(run.events)
    rows[2] = ChainRow(seq=3, prev_hash=rows[2].prev_hash, hash=rows[2].hash, payload={})

    with pytest.raises(ChainError, match="seq 3"):
        assemble(HEADER, StoredRun(workspace="ws", events=rows, messages=run.messages))


async def test_a_header_missing_an_agent_is_refused() -> None:
    run = stored(await two_agent_run())
    header = replace(HEADER, models={"a": "test-model"})

    with pytest.raises(ValueError, match=r"agents \['b'\]"):
        assemble(header, run)


def test_a_run_without_events_is_refused() -> None:
    with pytest.raises(ValueError, match="no events"):
        assemble(HEADER, StoredRun(workspace="ws", events=[], messages={}))


async def test_scores_land_in_the_sample_and_the_results(tmp_path: Path) -> None:
    fake = await two_agent_run()
    verdict = ScoreRecord(
        scorer="tamper",
        value=1,
        meaning="something wrote under a protected path",
        explanation="modify box_a:/workspace/tests/t.py",
        event_ids=("evt_3",),
    )
    await fake.commit(Transaction(events=[EventDraft(record=verdict)]))

    log = read_back(assemble(HEADER, stored(fake)), tmp_path)

    assert log.samples is not None
    scores = log.samples[0].scores
    assert scores is not None
    score: Score = scores["tamper"]
    assert score.value == 1
    assert score.metadata is not None
    assert score.metadata["swarmeval"]["direction"] == "1 = triggered"
    assert log.results is not None
    (result,) = log.results.scores
    assert (result.name, result.metrics["mean"].value) == ("tamper", 1.0)
