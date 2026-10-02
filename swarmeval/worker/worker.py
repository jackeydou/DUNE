"""The worker's main loop: claim queued runs and execute them, up to a concurrency limit.

M0 runs one worker. A run whose worker dies is marked `interrupted` when that worker (same
`owner_id`) starts again; M3 replaces this with leases and takeover.
"""

import asyncio
import logging

from swarmeval.control.queue import RunRow
from swarmeval.events import FencedError
from swarmeval.worker.run import Outcome, WorkerDeps, execute

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, deps: WorkerDeps, *, owner_id: str, max_runs: int = 4) -> None:
        self._deps = deps
        self._owner_id = owner_id
        self._slots = asyncio.Semaphore(max_runs)
        self._tasks: set[asyncio.Task[None]] = set()

    async def recover(self) -> list[str]:
        """Marks runs this worker owned before a restart as interrupted."""
        interrupted = await self._deps.queue.interrupt_owned(self._owner_id)
        for run_id in interrupted:
            log.warning("run %s was running when worker %s stopped", run_id, self._owner_id)
        return interrupted

    async def serve(self, poll_s: float = 1.0) -> None:
        """Claims and executes runs until cancelled."""
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
        await self._deps.queue.finish(run.run_id, run.owner_epoch, outcome.status, outcome.error)
        log.info("run %s: %s", run.run_id, outcome.status)
        return outcome
