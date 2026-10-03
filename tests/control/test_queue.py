"""Reruns of interrupted runs, against Postgres (docs/services/orchestrator.md#reruns)."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.queue import Queue, Recovered
from tests.queueing import enqueue, start

pytestmark = pytest.mark.docker


async def test_an_interrupted_run_is_rerun_at_the_next_epoch(
    engine: AsyncEngine, submission: str
) -> None:
    queue = Queue(engine)
    first, _ = await enqueue(queue, submission, epochs=2)
    owner_epoch = await start(engine, first, "w1")

    rerun = await queue.finish(first, owner_epoch, "interrupted", "sandboxd unavailable")

    assert rerun == f"c.{submission}.v0.e3"
    assert rerun is not None
    original, replacement = await queue.get(first), await queue.get(rerun)
    assert (original.status, original.error) == ("interrupted", "sandboxd unavailable")
    assert (replacement.status, replacement.replaces, replacement.owner_id) == (
        "queued",
        first,
        None,
    )
    assert (replacement.epoch, replacement.epochs, replacement.variant) == (3, 2, 0)
    assert (replacement.case_sha256, replacement.task_args, replacement.workspace) == (
        original.case_sha256,
        original.task_args,
        original.workspace,
    )


async def test_a_variant_gets_at_most_epochs_reruns(engine: AsyncEngine, submission: str) -> None:
    queue = Queue(engine)
    (run,) = await enqueue(queue, submission, epochs=1)

    rerun = await queue.finish(run, await start(engine, run, "w1"), "interrupted")
    assert rerun is not None
    again = await queue.finish(rerun, await start(engine, rerun, "w1"), "interrupted")

    assert again is None
    assert [r.status for r in await queue.list_runs(submission_id=submission)] == [
        "interrupted",
        "interrupted",
    ]


async def test_failed_and_cancelled_runs_are_not_rerun(
    engine: AsyncEngine, submission: str
) -> None:
    queue = Queue(engine)
    failed, cancelled = await enqueue(queue, submission, epochs=2)
    failed_epoch = await start(engine, failed, "w1")
    cancelled_epoch = await start(engine, cancelled, "w1")
    await queue.cancel(cancelled)

    assert await queue.finish(failed, failed_epoch, "failed", "backend 502") is None
    assert await queue.finish(cancelled, cancelled_epoch, "interrupted") is None

    assert [(r.run_id, r.status) for r in await queue.list_runs(submission_id=submission)] == [
        (failed, "failed"),
        (cancelled, "cancelled"),
    ]


async def test_a_stale_owner_cannot_finish_or_rerun(engine: AsyncEngine, submission: str) -> None:
    queue = Queue(engine)
    (run,) = await enqueue(queue, submission)
    owner_epoch = await start(engine, run, "w1")

    assert await queue.finish(run, owner_epoch - 1, "interrupted") is None

    assert [r.status for r in await queue.list_runs(submission_id=submission)] == ["running"]
    await queue.finish(run, owner_epoch, "done")


async def test_runs_interrupted_at_once_take_distinct_epochs(
    engine: AsyncEngine, submission: str
) -> None:
    queue = Queue(engine)
    runs = await enqueue(queue, submission, epochs=4)
    epochs = [await start(engine, r, f"w{i}") for i, r in enumerate(runs)]

    reruns = await asyncio.gather(
        *(queue.finish(r, e, "interrupted") for r, e in zip(runs, epochs, strict=True))
    )

    assert sorted(r or "" for r in reruns) == [f"c.{submission}.v0.e{e}" for e in (5, 6, 7, 8)]
    linked = {(await queue.get(r or "")).replaces for r in reruns}
    assert linked == set(runs)


async def test_a_restarted_worker_finishes_only_its_own_runs(
    engine: AsyncEngine, submission: str
) -> None:
    queue = Queue(engine)
    mine, cancelled, theirs = await enqueue(queue, submission, epochs=3)
    await start(engine, mine, "w_restarted")
    await start(engine, cancelled, "w_restarted")
    await queue.cancel(cancelled)
    their_epoch = await start(engine, theirs, "w_other")

    recovered = await queue.interrupt_owned("w_restarted")

    assert recovered == [
        Recovered(mine, "interrupted", f"c.{submission}.v0.e4"),
        Recovered(cancelled, "cancelled", None),
    ]
    assert (await queue.get(cancelled)).finished_at is not None
    assert (await queue.get(theirs)).status == "running"
    assert await queue.interrupt_owned("w_restarted") == []
    await queue.finish(theirs, their_epoch, "done")
