"""A run interrupted by an outage is rerun at a new epoch, and reports count the rerun."""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.analysis import load_summaries, report
from swarmeval.control.queue import RunRow
from swarmeval.events import ObjectStore
from swarmeval.worker import Worker, WorkerDeps
from swarmeval.worker import worker as worker_module
from swarmeval.worker.run import Outcome
from tests.queueing import enqueue

pytestmark = pytest.mark.docker


async def test_an_outage_reruns_the_run_and_reports_count_the_rerun(
    bare_deps: WorkerDeps, submission: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    (run_id,) = await enqueue(bare_deps.queue, bare_deps.engine, submission, epochs=1)

    async def outage_once(run: RunRow, deps: WorkerDeps) -> Outcome:
        if run.replaces is None:
            return Outcome("interrupted", "sandboxd unavailable")
        return Outcome("done")

    monkeypatch.setattr(worker_module, "execute", outage_once)
    outcomes = await Worker(bare_deps, owner_id="w_reruns").drain()

    rerun_id = f"c.{submission}.v0.e2"
    assert {r: o.status for r, o in outcomes.items()} == {
        run_id: "interrupted",
        rerun_id: "done",
    }
    result = report(load_summaries(bare_deps.store), [submission])
    assert [(c.requested, c.done, c.replaced, c.missing) for c in result.coverage] == [(1, 1, 1, 0)]
    summaries = {
        r["run_id"]: (r["replaces"], r["replaced_by"])
        for r in load_summaries(bare_deps.store).to_pylist()
        if r["submission_id"] == submission
    }
    assert summaries == {run_id: (None, rerun_id), rerun_id: (run_id, None)}


async def test_a_variant_out_of_reruns_shows_the_gap(
    bare_deps: WorkerDeps, submission: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await enqueue(bare_deps.queue, bare_deps.engine, submission, epochs=2)

    async def always_down(run: RunRow, deps: WorkerDeps) -> Outcome:
        return Outcome("interrupted", "model-gateway is unreachable")

    monkeypatch.setattr(worker_module, "execute", always_down)
    outcomes = await Worker(bare_deps, owner_id="w_reruns").drain()

    assert len(outcomes) == 4
    result = report(load_summaries(bare_deps.store), [submission])
    assert [(c.requested, c.done, c.replaced, c.missing) for c in result.coverage] == [(2, 0, 2, 2)]
    assert [(u.status, u.runs) for u in result.unscored] == [("interrupted", 4)]


async def test_a_summary_that_cannot_be_written_keeps_an_interrupted_run_and_its_rerun(
    bare_deps: WorkerDeps, submission: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    (run_id,) = await enqueue(bare_deps.queue, bare_deps.engine, submission, epochs=1)
    export_summary = worker_module.export_summary

    async def outage_once(run: RunRow, deps: WorkerDeps) -> Outcome:
        if run.replaces is None:
            return Outcome("interrupted", "sandboxd unavailable")
        return Outcome("done")

    async def first_summary_fails(engine: AsyncEngine, run: str, store: ObjectStore) -> str:
        if run == run_id:
            raise OSError("bucket unreachable")
        return await export_summary(engine, run, store)

    monkeypatch.setattr(worker_module, "execute", outage_once)
    monkeypatch.setattr(worker_module, "export_summary", first_summary_fails)
    outcomes = await Worker(bare_deps, owner_id="w_reruns").drain()

    rerun_id = f"c.{submission}.v0.e2"
    error = "sandboxd unavailable; summary export failed: OSError: bucket unreachable"
    assert outcomes == {run_id: Outcome("interrupted", error), rerun_id: Outcome("done")}
    original = await bare_deps.queue.get(run_id)
    assert (original.status, original.error) == ("interrupted", error)
    assert (await bare_deps.queue.get(rerun_id)).status == "done"
