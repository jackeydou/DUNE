"""The worker's main loop: claim queued runs and execute them, up to a concurrency limit.

Any number of workers share one queue; each claim takes a run no other worker holds, with a lease
the worker renews while it works on the run. A run whose worker dies is taken over by any worker
once its lease runs out, or finished by that worker (same `owner_id`) when it starts again. Until
a run can be resumed, taking it over means removing its sandboxes and marking it `interrupted`,
which reruns it at a new epoch (docs/services/orchestrator.md#leases-fencing-and-takeover).
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from swarmeval.control.queue import LEASE_S, Expired, Recovered, RunRow
from swarmeval.events import FencedError, export_summary
from swarmeval.sandbox import RunSandboxes, S3BlobStore, SandboxdError
from swarmeval.worker.leases import LeaseLost, Leases
from swarmeval.worker.run import Outcome, WorkerDeps, execute

log = logging.getLogger(__name__)

LOCK_CHECK_S = 10.0
"""How often a serving worker checks it still holds its id. A lost lock is noticed this late."""


class WorkerIdInUse(Exception):
    """Another live worker process has this worker id."""


class WorkerIdLost(Exception):
    """The connection holding this worker's id dropped while the worker served, so another
    process could take the id and interrupt this one's runs. The worker stopped serving."""


class WorkerHalted(Exception):
    """A run found this worker's host unfit to run any case (the isolation self-check failed).
    The worker claimed nothing more and stopped once its other runs finished."""


@asynccontextmanager
async def hold_worker_id(
    engine: AsyncEngine, owner_id: str, check_s: float = LOCK_CHECK_S
) -> AsyncGenerator[None]:
    """Holds a Postgres session-level advisory lock on `owner_id` for the block, on a connection
    of its own. A worker id must belong to one live process: at start a worker finishes every run
    its id owns, which would interrupt another live worker's runs. The lock goes when the
    connection does, so a crashed worker's id is free again at once.

    Every `check_s` the connection is checked to still be the database session that took the
    lock. If it dropped, the block is cancelled and `WorkerIdLost` raised in its place."""
    key = func.hashtextextended(f"swarmeval.worker:{owner_id}", 0)
    async with engine.connect() as conn:
        held, pid = (
            await conn.execute(select(func.pg_try_advisory_lock(key), func.pg_backend_pid()))
        ).one()
        await conn.commit()
        if not held:
            raise WorkerIdInUse(
                f"worker id `{owner_id}` is in use by another running worker. Give each worker "
                "process its own `--worker-id`, and reuse an id only to restart that worker."
            )
        holder = asyncio.current_task()
        assert holder is not None, "hold_worker_id runs inside a task"
        lost: list[str] = []
        watch = asyncio.create_task(_watch_lock(conn, pid, check_s, holder, lost))
        try:
            yield
        except asyncio.CancelledError as err:
            if not lost:
                raise
            holder.uncancel()
            raise WorkerIdLost(
                f"worker `{owner_id}` lost the database connection that holds its id "
                f"({lost[0]}), so another process could now start with the id and interrupt "
                "this one's runs. It stopped serving; its runs are interrupted and rerun when "
                "a worker with this id starts again."
            ) from err
        finally:
            watch.cancel()
            await asyncio.wait([watch])
            if not watch.cancelled():
                watch.result()
            if not lost:
                await conn.execute(select(func.pg_advisory_unlock(key)))
                await conn.commit()


async def _watch_lock(
    conn: AsyncConnection, pid: int, check_s: float, holder: asyncio.Task[Any], lost: list[str]
) -> None:
    """Cancels `holder`, saying why in `lost`, once `conn` is no longer database session `pid`.
    A dropped connection reconnects on its next use, as a session that holds no lock."""
    while True:
        await asyncio.sleep(check_s)
        try:
            now = (await conn.execute(select(func.pg_backend_pid()))).scalar_one()
            await conn.commit()
        except SQLAlchemyError as err:
            log.error("the connection holding the worker id failed", exc_info=True)
            lost.append(f"{type(err).__name__}: {err}".splitlines()[0])
        else:
            if now == pid:
                continue
            lost.append(f"it reconnected as session {now}, which does not hold the lock")
        holder.cancel()
        return


class Worker:
    def __init__(
        self, deps: WorkerDeps, *, owner_id: str, max_runs: int = 4, lease_s: float = LEASE_S
    ) -> None:
        self._deps = deps
        self._owner_id = owner_id
        self._leases = Leases(deps.queue, owner_id, lease_s)
        self._slots = asyncio.Semaphore(max_runs)
        self._tasks: set[asyncio.Task[None]] = set()
        self._halt: str | None = None
        """Why this worker claims no more runs, once a run found its host broken."""

    async def recover(self) -> list[Recovered]:
        """Finishes the runs this worker owned before a restart (interrupted ones get reruns),
        removes their sandboxes, and writes their summaries. Raises if a summary cannot be
        written: the worker does not start."""
        recovered = await self._deps.queue.interrupt_owned(self._owner_id)
        for run in recovered:
            log.warning(
                "run %s was unfinished when worker %s stopped; now %s, rerun as %s",
                run.run_id,
                self._owner_id,
                run.status,
                run.replacement or "nothing",
            )
            await self._remove_sandboxes(run.run_id)
            await export_summary(self._deps.engine, run.run_id, self._deps.store)
        return recovered

    async def serve(self, poll_s: float = 1.0) -> None:
        """Takes over runs whose lease ran out, and claims and executes queued runs, until
        cancelled. Raises `WorkerIdInUse`, before touching any run, when another live worker has
        this worker's id, and `WorkerIdLost` if it stops holding the id. When a run finds this
        host broken, claims nothing more, lets its other runs finish, and raises
        `WorkerHalted`."""
        queue, lease_s = self._deps.queue, self._leases.lease_s
        async with hold_worker_id(self._deps.engine, self._owner_id), self._leases.renewing():
            await self.recover()
            try:
                while self._halt is None:
                    await self._slots.acquire()
                    if self._halt is not None:
                        break
                    work: Coroutine[Any, Any, Outcome]
                    if (expired := await queue.claim_expired(self._owner_id, lease_s)) is not None:
                        work = self._take_over(expired)
                    elif (run := await queue.claim(self._owner_id, lease_s)) is not None:
                        work = self._execute(run)
                    else:
                        self._slots.release()
                        await asyncio.sleep(poll_s)
                        continue
                    task = asyncio.create_task(self._in_slot(work))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                await asyncio.gather(*self._tasks)
            finally:
                for task in self._tasks:
                    task.cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)
        raise WorkerHalted(self._halt)

    async def drain(self) -> dict[str, Outcome]:
        """Executes queued runs one at a time until none is left, or until a run finds this host
        broken. Takes over no run. For tests and one-shot use."""
        outcomes: dict[str, Outcome] = {}
        async with self._leases.renewing():
            while (
                self._halt is None
                and (run := await self._deps.queue.claim(self._owner_id, self._leases.lease_s))
                is not None
            ):
                outcomes[run.run_id] = await self._execute(run)
        return outcomes

    async def _in_slot(self, work: Coroutine[Any, Any, Outcome]) -> None:
        try:
            await work
        finally:
            self._slots.release()

    async def _take_over(self, expired: Expired) -> Outcome:
        """Finishes a run whose owner stopped renewing its lease: removes its sandboxes and
        marks it `interrupted`, which queues its rerun, or finishes it `cancelled` if it was
        cancelled while it ran. Resuming it instead is not built yet."""
        run = expired.run
        log.warning(
            "run %s: worker %s's lease ran out at %s; worker %s took it over at owner_epoch %d",
            run.run_id,
            expired.previous_owner,
            expired.expired_at.isoformat(),
            self._owner_id,
            run.owner_epoch,
        )
        try:
            await self._leases.hold(run.run_id, run.owner_epoch, self._remove_sandboxes(run.run_id))
        except LeaseLost as err:
            log.warning("run %s: %s", run.run_id, err)
            return Outcome("interrupted", str(err))
        if run.status == "cancelled":
            outcome = Outcome("cancelled")
        else:
            outcome = Outcome(
                "interrupted",
                f"worker `{expired.previous_owner}` stopped renewing its lease, which ran out at "
                f"{expired.expired_at.isoformat()}; worker `{self._owner_id}` took the run over. "
                "Resuming a run is not built yet, so it is rerun as a new epoch.",
            )
        return await _to_the_end(self._record(run, outcome))

    async def _remove_sandboxes(self, run_id: str) -> None:
        """Removes whatever this worker's sandboxd holds of a run another process owned.
        Containers live on the node that created them, so a sandboxd on another node finds
        none, and they are left until the old worker's id restarts there. A sandboxd that cannot
        remove them leaves them too, labeled with the run; the run's evidence is in Postgres
        either way."""
        try:
            await RunSandboxes(self._deps.sandboxd, run_id, S3BlobStore(self._deps.store)).destroy()
        except SandboxdError:
            log.warning(
                "run %s: could not remove its sandboxes; containers labeled "
                "swarmeval.run_id=%s may be left on the host",
                run_id,
                run_id,
                exc_info=True,
            )

    async def _execute(self, run: RunRow) -> Outcome:
        log.info("run %s: claimed at owner_epoch %d", run.run_id, run.owner_epoch)
        try:
            outcome = await self._leases.hold(run.run_id, run.owner_epoch, execute(run, self._deps))
        except LeaseLost as err:
            log.warning("run %s: %s", run.run_id, err)
            return Outcome("interrupted", str(err))
        except FencedError:
            log.warning("run %s: another worker owns it now; dropping it", run.run_id)
            return Outcome("interrupted", "fenced")
        except Exception as err:
            # The task boundary: an unexpected error fails this run, not the worker.
            log.error("run %s failed unexpectedly", run.run_id, exc_info=True)
            outcome = Outcome("failed", f"{type(err).__name__}: {err}")
        if outcome.host_fault:
            # Before recording, which may be slow: a slot freed meanwhile must not claim a run.
            self._halt = (
                f"run `{run.run_id}` found worker `{self._owner_id}`'s host unfit to run any "
                f"case: {outcome.error} Every run this worker claimed would fail the same way, "
                "so it claims no more. Fix the host, then restart the worker."
            )
            log.error("%s", self._halt)
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
            # Same boundary: the run stays finished and says why reports cannot see it.
            log.error("run %s: summary export failed", run.run_id, exc_info=True)
            error = f"summary export failed: {type(err).__name__}: {err}"
            status = await self._deps.queue.summary_failed(run.run_id, run.owner_epoch, error)
            outcome = replace(
                outcome,
                status=status or outcome.status,
                error="; ".join(e for e in (outcome.error, error) if e),
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
