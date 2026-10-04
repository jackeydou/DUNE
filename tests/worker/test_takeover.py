"""Leases and takeover between workers on one Postgres
(docs/services/orchestrator.md#leases-fencing-and-takeover).

`execute` and the sandboxd client are replaced: what is under test is renewing, taking over, and
finishing, not the run itself."""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import timedelta
from typing import Any

import grpc
import pytest
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.analysis import load_summaries
from swarmeval.control.queue import FINISHED, Queue, RunRow
from swarmeval.control.service import to_proto
from swarmeval.db import control_runs
from swarmeval.events import ObjectStore
from swarmeval.sandbox import BlobStore
from swarmeval.worker import Worker, WorkerDeps
from swarmeval.worker import worker as worker_module
from swarmeval.worker.run import Outcome
from tests.queueing import enqueue, lease, start

pytestmark = pytest.mark.docker


@pytest.fixture
def removed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Runs whose sandboxes a worker asked sandboxd to remove."""
    calls: list[str] = []

    class Sandboxes:
        def __init__(self, channel: grpc.aio.Channel, run_id: str, blobs: BlobStore) -> None:
            self._run_id = run_id

        async def destroy(self) -> None:
            calls.append(self._run_id)

    monkeypatch.setattr(worker_module, "RunSandboxes", Sandboxes)
    return calls


def _fake_execute(
    monkeypatch: pytest.MonkeyPatch, body: Callable[[RunRow], Coroutine[Any, Any, Outcome]]
) -> list[str]:
    executed: list[str] = []

    async def fake_execute(run: RunRow, deps: WorkerDeps) -> Outcome:
        executed.append(run.run_id)
        return await body(run)

    monkeypatch.setattr(worker_module, "execute", fake_execute)
    return executed


async def _until(queue: Queue, run_id: str, done: Callable[[RunRow], bool]) -> RunRow:
    async with asyncio.timeout(30):
        while not done(run := await queue.get(run_id)):
            await asyncio.sleep(0.02)
    return run


async def _summaries(store: ObjectStore, submission: str, runs: int) -> dict[str, str]:
    """Each run's summary status, once `runs` summaries of the submission are written."""
    async with asyncio.timeout(30):
        while True:
            try:
                found = {
                    s["run_id"]: s["status"]
                    for s in load_summaries(store).to_pylist()
                    if s["submission_id"] == submission
                }
            except FileNotFoundError:
                found = {}
            if len(found) == runs:
                return found
            await asyncio.sleep(0.05)


@pytest.fixture
async def serving() -> AsyncIterator[Callable[[Worker], None]]:
    """Starts workers serving; stops them at teardown."""
    tasks: list[asyncio.Task[None]] = []
    yield lambda worker: tasks.append(asyncio.create_task(worker.serve(poll_s=0.02)))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_a_dead_workers_run_is_taken_over_and_rerun(
    bare_deps: WorkerDeps,
    submission: str,
    removed: list[str],
    serving: Callable[[Worker], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def done(run: RunRow) -> Outcome:
        return Outcome("done")

    executed = _fake_execute(monkeypatch, done)
    (run_id,) = await enqueue(bare_deps.queue, bare_deps.engine, submission)
    await lease(bare_deps.engine, run_id, "w_dead", -1)

    serving(Worker(bare_deps, owner_id="w_taker", lease_s=1))
    taken = await _until(bare_deps.queue, run_id, lambda r: r.status in FINISHED)
    rerun_id = f"c.{submission}.v0.e2"
    rerun = await _until(bare_deps.queue, rerun_id, lambda r: r.status in FINISHED)

    assert (taken.status, taken.owner_id) == ("interrupted", "w_taker")
    assert taken.error is not None
    assert "worker `w_dead` stopped renewing its lease" in taken.error
    assert "worker `w_taker` took the run over" in taken.error
    assert (rerun.status, rerun.replaces) == ("done", run_id)
    # The run that changed hands says so, also through the Control API; its rerun started clean.
    assert (taken.takeovers, rerun.takeovers) == (1, 0)
    assert to_proto(taken).takeovers == 1
    assert executed == [rerun_id]
    assert removed == [run_id]
    assert await _summaries(bare_deps.store, submission, 2) == {
        run_id: "interrupted",
        rerun_id: "done",
    }


async def test_a_run_cancelled_while_its_worker_was_dead_is_finished_cancelled(
    bare_deps: WorkerDeps,
    submission: str,
    removed: list[str],
    serving: Callable[[Worker], None],
) -> None:
    (run_id,) = await enqueue(bare_deps.queue, bare_deps.engine, submission)
    await lease(bare_deps.engine, run_id, "w_dead", -1)
    await bare_deps.queue.cancel(run_id)

    serving(Worker(bare_deps, owner_id="w_taker", lease_s=1))
    finished = await _until(bare_deps.queue, run_id, lambda r: r.finished_at is not None)

    assert (finished.status, finished.error) == ("cancelled", None)
    assert removed == [run_id]
    assert [r.run_id for r in await bare_deps.queue.list_runs(submission_id=submission)] == [run_id]
    assert await _summaries(bare_deps.store, submission, 1) == {run_id: "cancelled"}


async def test_a_live_worker_keeps_its_run_past_many_leases(
    bare_deps: WorkerDeps,
    submission: str,
    removed: list[str],
    serving: Callable[[Worker], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow(run: RunRow) -> Outcome:
        await asyncio.sleep(1.5)
        return Outcome("done")

    executed = _fake_execute(monkeypatch, slow)
    (run_id,) = await enqueue(bare_deps.queue, bare_deps.engine, submission)

    serving(Worker(bare_deps, owner_id="w_slow", lease_s=0.3))
    await _until(bare_deps.queue, run_id, lambda r: r.status == "running")
    serving(Worker(bare_deps, owner_id="w_watching", lease_s=0.3))
    finished = await _until(bare_deps.queue, run_id, lambda r: r.status in FINISHED)

    assert (finished.status, finished.owner_id, finished.owner_epoch) == ("done", "w_slow", 1)
    assert executed == [run_id]
    assert removed == []


async def test_a_worker_whose_run_was_taken_over_stops_it_and_records_nothing(
    bare_deps: WorkerDeps,
    submission: str,
    serving: Callable[[Worker], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, stopped = asyncio.Event(), asyncio.Event()

    async def forever(run: RunRow) -> Outcome:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stopped.set()
            raise
        return Outcome("done")

    _fake_execute(monkeypatch, forever)
    (run_id,) = await enqueue(bare_deps.queue, bare_deps.engine, submission)
    serving(Worker(bare_deps, owner_id="w_paused", lease_s=0.3))
    await asyncio.wait_for(started.wait(), 10)

    # As if the worker had stalled past its lease and another had claimed the run meanwhile.
    async with bare_deps.engine.begin() as conn:
        await conn.execute(
            update(control_runs)
            .where(control_runs.c.run_id == run_id)
            .values(lease_until=func.now() - timedelta(seconds=1))
        )
    taken = await bare_deps.queue.claim_expired("w_other", lease_s=30)
    assert taken is not None
    assert taken.run.run_id == run_id
    await asyncio.wait_for(stopped.wait(), 5)
    await asyncio.sleep(0.2)

    after = await bare_deps.queue.get(run_id)
    assert (after.status, after.owner_id, after.finished_at) == ("running", "w_other", None)
    await bare_deps.queue.finish(run_id, taken.run.owner_epoch, "done")


async def test_a_restarted_worker_removes_the_sandboxes_of_the_runs_it_finishes(
    bare_deps: WorkerDeps, submission: str, removed: list[str]
) -> None:
    (run_id,) = await enqueue(bare_deps.queue, bare_deps.engine, submission)
    await start(bare_deps.engine, run_id, "w_restarted_sandboxes")

    (recovered,) = await Worker(bare_deps, owner_id="w_restarted_sandboxes").recover()

    assert (recovered.run_id, recovered.status) == (run_id, "interrupted")
    assert removed == [run_id]
    assert recovered.replacement is not None
    await bare_deps.queue.cancel(recovered.replacement)


async def test_an_owner_taken_over_after_its_run_ended_writes_no_status_or_summary(
    bare_deps: WorkerDeps, submission: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    (run_id,) = await enqueue(bare_deps.queue, bare_deps.engine, submission)
    exported: list[str] = []
    real_export = worker_module.export_summary

    async def export(engine: AsyncEngine, run: str, store: ObjectStore) -> str:
        exported.append(run)
        return await real_export(engine, run, store)

    async def taken_over_on_the_way_out(run: RunRow) -> Outcome:
        # As if the owner had stalled between the end of the run and its final status.
        async with bare_deps.engine.begin() as conn:
            await conn.execute(
                update(control_runs)
                .where(control_runs.c.run_id == run.run_id)
                .values(lease_until=func.now() - timedelta(seconds=1))
            )
        assert await bare_deps.queue.claim_expired("w_other") is not None
        return Outcome("done")

    _fake_execute(monkeypatch, taken_over_on_the_way_out)
    monkeypatch.setattr(worker_module, "export_summary", export)

    outcomes = await Worker(bare_deps, owner_id="w_stalled").drain()

    assert outcomes == {run_id: Outcome("interrupted", "fenced")}
    after = await bare_deps.queue.get(run_id)
    assert (after.status, after.owner_id, after.finished_at) == ("running", "w_other", None)
    assert exported == []
    await bare_deps.queue.finish(run_id, after.owner_epoch, "done")
