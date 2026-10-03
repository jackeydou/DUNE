"""The worker's main loop: claim queued runs and execute them, up to a concurrency limit.

Any number of workers share one queue; each claim takes a run no other worker holds. A run whose
worker dies is marked `interrupted`, and rerun at a new epoch, when that worker (same `owner_id`)
starts again; M3 replaces this with leases and takeover.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.queue import Recovered, RunRow
from swarmeval.events import FencedError, export_summary
from swarmeval.worker.run import Outcome, WorkerDeps, execute

log = logging.getLogger(__name__)


class WorkerIdInUse(Exception):
    """Another live worker process has this worker id."""


@asynccontextmanager
async def hold_worker_id(engine: AsyncEngine, owner_id: str) -> AsyncGenerator[None]:
    """Holds a Postgres session-level advisory lock on `owner_id` for the block, on a connection
    of its own. A worker id must belong to one live process: at start a worker finishes every run
    its id owns, which would interrupt another live worker's runs. The lock goes when the
    connection does, so a crashed worker's id is free again at once."""
    key = func.hashtextextended(f"swarmeval.worker:{owner_id}", 0)
    async with engine.connect() as conn:
        held = (await conn.execute(select(func.pg_try_advisory_lock(key)))).scalar_one()
        await conn.commit()
        if not held:
            raise WorkerIdInUse(
                f"worker id `{owner_id}` is in use by another running worker. Give each worker "
                "process its own `--worker-id`, and reuse an id only to restart that worker."
            )
        try:
            yield
        finally:
            await conn.execute(select(func.pg_advisory_unlock(key)))
            await conn.commit()


class Worker:
    def __init__(self, deps: WorkerDeps, *, owner_id: str, max_runs: int = 4) -> None:
        self._deps = deps
        self._owner_id = owner_id
        self._slots = asyncio.Semaphore(max_runs)
        self._tasks: set[asyncio.Task[None]] = set()

    async def recover(self) -> list[Recovered]:
        """Finishes the runs this worker owned before a restart (interrupted ones get reruns),
        and writes their summaries. Raises if a summary cannot be written: the worker does not
        start."""
        recovered = await self._deps.queue.interrupt_owned(self._owner_id)
        for run in recovered:
            log.warning(
                "run %s was unfinished when worker %s stopped; now %s, rerun as %s",
                run.run_id,
                self._owner_id,
                run.status,
                run.replacement or "nothing",
            )
            await export_summary(self._deps.engine, run.run_id, self._deps.store)
        return recovered

    async def serve(self, poll_s: float = 1.0) -> None:
        """Claims and executes runs until cancelled. Raises `WorkerIdInUse`, before touching any
        run, when another live worker has this worker's id."""
        async with hold_worker_id(self._deps.engine, self._owner_id):
            await self.recover()
            try:
                while True:
                    await self._slots.acquire()
                    run = await self._deps.queue.claim(self._owner_id)
                    if run is None:
                        self._slots.release()
                        await asyncio.sleep(poll_s)
                        continue
                    task = asyncio.create_task(self._run(run))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
            finally:
                for task in self._tasks:
                    task.cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)

    async def drain(self) -> dict[str, Outcome]:
        """Executes queued runs one at a time until none is left. For tests and one-shot use."""
        outcomes: dict[str, Outcome] = {}
        while (run := await self._deps.queue.claim(self._owner_id)) is not None:
            outcomes[run.run_id] = await self._execute(run)
        return outcomes

    async def _run(self, run: RunRow) -> None:
        try:
            await self._execute(run)
        finally:
            self._slots.release()

    async def _execute(self, run: RunRow) -> Outcome:
        log.info("run %s: claimed at owner_epoch %d", run.run_id, run.owner_epoch)
        try:
            outcome = await execute(run, self._deps)
        except FencedError:
            log.warning("run %s: another worker owns it now; dropping it", run.run_id)
            return Outcome("interrupted", "fenced")
        except Exception as err:
            # The task boundary: an unexpected error fails this run, not the worker.
            log.error("run %s failed unexpectedly", run.run_id, exc_info=True)
            outcome = Outcome("failed", f"{type(err).__name__}: {err}")
        return await _to_the_end(self._record(run, outcome))

    async def _record(self, run: RunRow, outcome: Outcome) -> Outcome:
        """Writes the run's final status, then its summary."""
        rerun = await self._deps.queue.finish(
            run.run_id, run.owner_epoch, outcome.status, outcome.error
        )
        if rerun is not None:
            log.warning("run %s interrupted; queued %s to rerun it", run.run_id, rerun)
        try:
            await export_summary(self._deps.engine, run.run_id, self._deps.store)
        except Exception as err:
            # Same boundary: a run reports cannot see is failed, saying why, like a failed
            # export. `finish` keeps a run that was cancelled meanwhile `cancelled`.
            log.error("run %s: summary export failed", run.run_id, exc_info=True)
            outcome = Outcome("failed", f"summary export failed: {type(err).__name__}: {err}")
            await self._deps.queue.finish(
                run.run_id, run.owner_epoch, outcome.status, outcome.error
            )
        log.info("run %s: %s", run.run_id, outcome.status)
        return outcome


async def _to_the_end[T](work: Coroutine[Any, Any, T]) -> T:
    """Runs `work` to its end even when the caller is cancelled meanwhile, then lets the cancel
    through. A worker that stops between a run's final status and its summary would leave a
    finished run no restart revisits and no report sees. A second cancel abandons `work`."""
    task = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
