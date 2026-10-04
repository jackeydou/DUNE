"""Leases and takeover in the queue, against Postgres
(docs/services/orchestrator.md#leases-fencing-and-takeover)."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.queue import Queue
from swarmeval.db import control_runs
from swarmeval.events import FencedError, PostgresRunStore
from swarmeval.runtime.records import AlertRecord, EventDraft, Transaction
from tests.queueing import enqueue, lease, start

pytestmark = pytest.mark.docker


async def _lease_left(engine: AsyncEngine, run_id: str) -> float:
    """Seconds until the run's lease runs out, by the database's clock."""
    runs = control_runs.c
    async with engine.connect() as conn:
        left = select(func.extract("epoch", runs.lease_until - func.now()))
        return float((await conn.execute(left.where(runs.run_id == run_id))).scalar_one())


async def test_a_claim_takes_a_lease(engine: AsyncEngine, submission: str) -> None:
    queue = Queue(engine)
    (queued,) = await enqueue(queue, engine, submission)

    claimed = await queue.claim("w_lease", lease_s=20)

    assert claimed is not None
    assert claimed.run_id == queued
    assert 19 < await _lease_left(engine, queued) <= 20
    await queue.finish(queued, claimed.owner_epoch, "done")


async def test_renewal_extends_only_the_leases_still_held(
    engine: AsyncEngine, submission: str
) -> None:
    queue = Queue(engine)
    held, stale, done, theirs = await enqueue(queue, engine, submission, epochs=4)
    held_epoch = await lease(engine, held, "w_renew", 1)
    stale_epoch = await lease(engine, stale, "w_renew", 1)
    done_epoch = await lease(engine, done, "w_renew", 1)
    their_epoch = await lease(engine, theirs, "w_other", 1)
    await queue.finish(done, done_epoch, "done")

    renewed = await queue.renew(
        "w_renew",
        {held: held_epoch, stale: stale_epoch - 1, done: done_epoch, theirs: their_epoch},
        lease_s=20,
    )

    assert renewed == {held}
    assert await _lease_left(engine, held) > 19
    assert await _lease_left(engine, stale) <= 1
    assert await _lease_left(engine, theirs) <= 1
    assert await queue.renew("w_renew", {}) == set()
    for run_id, epoch in ((held, held_epoch), (stale, stale_epoch), (theirs, their_epoch)):
        await queue.finish(run_id, epoch, "done")


async def test_a_run_whose_lease_ran_out_is_claimed_and_its_owner_fenced(
    engine: AsyncEngine, submission: str
) -> None:
    queue = Queue(engine)
    expired, live, legacy = await enqueue(queue, engine, submission, epochs=3)
    old_epoch = await lease(engine, expired, "w_dead", -1)
    live_epoch = await lease(engine, live, "w_alive", 30)
    legacy_epoch = await start(engine, legacy, "w_legacy")
    stale = PostgresRunStore(
        engine, run_id=expired, workspace="ws_test", owner_epoch=old_epoch, sandboxes={}
    )

    taken = await queue.claim_expired("w_taker", lease_s=20)

    assert taken is not None
    assert (taken.run.run_id, taken.run.status) == (expired, "running")
    assert (taken.run.owner_id, taken.run.owner_epoch) == ("w_taker", old_epoch + 1)
    assert taken.previous_owner == "w_dead"
    assert taken.expired_at < datetime.now(UTC)
    assert await _lease_left(engine, expired) > 19
    assert await queue.claim_expired("w_taker") is None
    with pytest.raises(FencedError, match=expired):
        await queue.finish(expired, old_epoch, "done")
    assert await queue.renew("w_dead", {expired: old_epoch}) == set()
    with pytest.raises(FencedError, match=expired):
        alert = AlertRecord(message="late", severity="low")
        await stale.commit(Transaction(events=[EventDraft(record=alert)]))

    assert await queue.finish(expired, taken.run.owner_epoch, "interrupted") is not None
    await queue.finish(live, live_epoch, "done")
    await queue.finish(legacy, legacy_epoch, "done")


async def test_a_run_cancelled_while_its_owner_died_is_claimed_as_cancelled(
    engine: AsyncEngine, submission: str
) -> None:
    queue = Queue(engine)
    (run,) = await enqueue(queue, engine, submission)
    await lease(engine, run, "w_dead", -1)
    await queue.cancel(run)

    taken = await queue.claim_expired("w_taker")

    assert taken is not None
    assert (taken.run.run_id, taken.run.status) == (run, "cancelled")
    assert await queue.finish(run, taken.run.owner_epoch, "interrupted") is None
    finished = await queue.get(run)
    assert finished.status == "cancelled"
    assert finished.finished_at is not None


async def test_two_takers_never_claim_the_same_run(engine: AsyncEngine, submission: str) -> None:
    queue = Queue(engine)
    runs = await enqueue(queue, engine, submission, epochs=6)
    for run_id in runs:
        await lease(engine, run_id, "w_dead", -1)

    claims = await asyncio.gather(*(queue.claim_expired(f"w_taker{i}") for i in range(8)))

    taken = [c.run.run_id for c in claims if c is not None]
    assert sorted(taken) == sorted(runs)
    for claim in claims:
        if claim is not None:
            await queue.finish(claim.run.run_id, claim.run.owner_epoch, "done")
