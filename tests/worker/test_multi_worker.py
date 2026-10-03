"""Several workers on one Postgres: each run is claimed and executed by exactly one of them.

`execute` is replaced, so no sandbox or model is involved; what is under test is the queue, the
worker loop, finishing, reruns, and summaries."""

import asyncio
import random
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.analysis import load_summaries, report
from swarmeval.control.queue import FINISHED, Queue, RunRow
from swarmeval.db import async_engine
from swarmeval.events import ObjectStore
from swarmeval.worker import (
    Worker,
    WorkerDeps,
    WorkerHalted,
    WorkerIdInUse,
    WorkerIdLost,
    hold_worker_id,
)
from swarmeval.worker import worker as worker_module
from swarmeval.worker.run import Outcome
from tests.queueing import enqueue, start

pytestmark = pytest.mark.docker


@pytest.fixture
async def other_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    """A second pool, as a second worker process would have."""
    engine = async_engine(postgres_url)
    yield engine
    await engine.dispose()


async def _until_finished(queue: Queue, submission: str, runs: int) -> list[RunRow]:
    async with asyncio.timeout(60):
        while True:
            found = await queue.list_runs(submission_id=submission, limit=1000)
            if len(found) == runs and all(r.status in FINISHED for r in found):
                return found
            await asyncio.sleep(0.05)


async def test_two_workers_claim_each_run_exactly_once(
    bare_deps: WorkerDeps,
    other_engine: AsyncEngine,
    submission: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = await enqueue(bare_deps.queue, submission, variants=3, epochs=8)
    flaky = queued[0]
    executed: list[tuple[str, str | None]] = []

    async def fake_execute(run: RunRow, deps: WorkerDeps) -> Outcome:
        executed.append((run.run_id, run.owner_id))
        await asyncio.sleep(random.uniform(0, 0.02))
        if run.run_id == flaky:
            return Outcome("interrupted", "sandboxd unavailable")
        return Outcome("done")

    monkeypatch.setattr(worker_module, "execute", fake_execute)
    second = replace(bare_deps, engine=other_engine, queue=Queue(other_engine))
    workers = [
        Worker(bare_deps, owner_id="w_multi_a", max_runs=3),
        Worker(second, owner_id="w_multi_b", max_runs=3),
    ]
    serving = [asyncio.create_task(w.serve(poll_s=0.02)) for w in workers]
    try:
        finished = await _until_finished(bare_deps.queue, submission, len(queued) + 1)
    finally:
        for task in serving:
            task.cancel()
        await asyncio.gather(*serving, return_exceptions=True)

    runs = Counter(run_id for run_id, _ in executed)
    assert set(runs.values()) == {1}
    assert set(runs) == {r.run_id for r in finished}
    by_owner = Counter(owner for _, owner in executed)
    assert set(by_owner) == {"w_multi_a", "w_multi_b"}
    assert {r.run_id: r.owner_id for r in finished} == dict(executed)
    rerun = next(r for r in finished if r.replaces == flaky)
    assert (rerun.status, rerun.epoch) == ("done", 9)
    assert Counter(r.status for r in finished) == {"done": len(queued), "interrupted": 1}

    summaries = [
        s for s in load_summaries(bare_deps.store).to_pylist() if s["submission_id"] == submission
    ]
    assert sorted(s["run_id"] for s in summaries) == sorted(runs)
    coverage = report(load_summaries(bare_deps.store), [submission]).coverage
    assert [(c.variant, c.requested, c.done, c.replaced) for c in coverage] == [
        (0, 8, 8, 1),
        (1, 8, 8, 0),
        (2, 8, 8, 0),
    ]


async def test_a_worker_id_is_held_by_one_worker_at_a_time(
    engine: AsyncEngine, other_engine: AsyncEngine
) -> None:
    async with hold_worker_id(engine, "w_held"):
        with pytest.raises(WorkerIdInUse, match="w_held"):
            async with hold_worker_id(other_engine, "w_held"):
                pass
        async with hold_worker_id(other_engine, "w_other"):
            pass

    async with hold_worker_id(other_engine, "w_held"):
        pass


async def test_a_second_worker_with_a_live_id_leaves_its_runs_alone(
    bare_deps: WorkerDeps, other_engine: AsyncEngine, submission: str
) -> None:
    (run_id,) = await enqueue(bare_deps.queue, submission)
    owner_epoch = await start(bare_deps.engine, run_id, "w_twin")
    duplicate = Worker(
        replace(bare_deps, engine=other_engine, queue=Queue(other_engine)), owner_id="w_twin"
    )

    async with hold_worker_id(bare_deps.engine, "w_twin"):
        with pytest.raises(WorkerIdInUse):
            await duplicate.serve(poll_s=0.02)

    assert (await bare_deps.queue.get(run_id)).status == "running"
    await bare_deps.queue.finish(run_id, owner_epoch, "done")


async def test_a_worker_stopped_while_recording_a_run_still_writes_its_summary(
    bare_deps: WorkerDeps, submission: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    (run_id,) = await enqueue(bare_deps.queue, submission)
    recording = asyncio.Event()
    export_summary = worker_module.export_summary

    async def done(run: RunRow, deps: WorkerDeps) -> Outcome:
        return Outcome("done")

    async def slow_summary(engine: AsyncEngine, run_id: str, store: ObjectStore) -> str:
        recording.set()
        await asyncio.sleep(0.2)
        return await export_summary(engine, run_id, store)

    monkeypatch.setattr(worker_module, "execute", done)
    monkeypatch.setattr(worker_module, "export_summary", slow_summary)
    serving = asyncio.create_task(Worker(bare_deps, owner_id="w_stopped").serve(poll_s=0.02))
    await asyncio.wait_for(recording.wait(), 10)
    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving

    statuses = [
        s["status"] for s in load_summaries(bare_deps.store).to_pylist() if s["run_id"] == run_id
    ]
    assert statuses == ["done"]


async def test_a_worker_that_loses_its_id_lock_stops_serving(
    engine: AsyncEngine, other_engine: AsyncEngine
) -> None:
    async def serve() -> None:
        async with hold_worker_id(engine, "w_lost", check_s=0.05):
            await asyncio.sleep(60)

    serving = asyncio.create_task(serve())
    holder = text(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 1 "
        "AND ((classid::bigint << 32) | objid::bigint) = hashtextextended(:name, 0)"
    )
    async with asyncio.timeout(10), other_engine.connect() as conn:
        name = {"name": "swarmeval.worker:w_lost"}
        while (pid := (await conn.execute(holder, name)).scalar()) is None:
            await asyncio.sleep(0.02)
        await conn.execute(select(func.pg_terminate_backend(pid)))

    done, _ = await asyncio.wait({serving}, timeout=10)
    assert done, "still serving after its id's lock was lost"
    with pytest.raises(WorkerIdLost, match="`w_lost` lost the database connection"):
        await serving
    async with hold_worker_id(other_engine, "w_lost"):
        pass


async def test_a_run_that_finds_the_host_unisolated_stops_the_worker_claiming(
    bare_deps: WorkerDeps, submission: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken, in_flight, *untouched = await enqueue(bare_deps.queue, submission, epochs=4)
    executed: list[str] = []
    leak = (
        "the isolation self-check failed before any agent turn: sandbox `a` and sandbox `b`: "
        "proc got through (pid 7 carries its marker)."
    )

    started = asyncio.Event()

    async def fake_execute(run: RunRow, deps: WorkerDeps) -> Outcome:
        executed.append(run.run_id)
        if run.run_id == broken:
            await started.wait()
            return Outcome("failed", leak, host_fault=True)
        started.set()
        await asyncio.sleep(0.3)
        return Outcome("done")

    monkeypatch.setattr(worker_module, "execute", fake_execute)
    worker = Worker(bare_deps, owner_id="w_unisolated", max_runs=2)

    with pytest.raises(WorkerHalted) as info:
        await asyncio.wait_for(worker.serve(poll_s=0.02), 30)

    message = str(info.value)
    assert f"run `{broken}`" in message
    assert "sandbox `a` and sandbox `b`: proc got through" in message
    assert executed == [broken, in_flight]
    runs = {r.run_id: r.status for r in await bare_deps.queue.list_runs(submission_id=submission)}
    assert runs == {broken: "failed", in_flight: "done", **dict.fromkeys(untouched, "queued")}


async def test_a_slot_freed_while_a_host_fault_is_recorded_claims_nothing(
    bare_deps: WorkerDeps, submission: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken, in_flight, *untouched = await enqueue(bare_deps.queue, submission, epochs=4)
    executed: list[str] = []
    in_flight_done = asyncio.Event()

    async def fake_execute(run: RunRow, deps: WorkerDeps) -> Outcome:
        executed.append(run.run_id)
        if run.run_id == broken:
            return Outcome("failed", "proc got through", host_fault=True)
        await asyncio.sleep(0.05)
        in_flight_done.set()
        return Outcome("done")

    real_export = worker_module.export_summary

    async def slow_export(engine: AsyncEngine, run_id: str, store: ObjectStore) -> None:
        if run_id == broken:
            # Recording the faulty run outlasts the other run, whose slot frees meanwhile.
            await in_flight_done.wait()
            await asyncio.sleep(0.2)
        await real_export(engine, run_id, store)

    monkeypatch.setattr(worker_module, "execute", fake_execute)
    monkeypatch.setattr(worker_module, "export_summary", slow_export)
    worker = Worker(bare_deps, owner_id="w_slow_record", max_runs=2)

    with pytest.raises(WorkerHalted):
        await asyncio.wait_for(worker.serve(poll_s=0.02), 30)

    assert executed == [broken, in_flight]
    runs = {r.run_id: r.status for r in await bare_deps.queue.list_runs(submission_id=submission)}
    assert runs == {broken: "failed", in_flight: "done", **dict.fromkeys(untouched, "queued")}
