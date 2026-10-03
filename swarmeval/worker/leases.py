"""The leases of the runs one worker holds.

Behavior: docs/services/orchestrator.md#leases-fencing-and-takeover.

A claim gives a run a lease of `lease_s`. While the worker works on the run, it renews every lease
it holds each third of that. A run the renewal no longer finds was taken over or finished
elsewhere, and is stopped here at once. When renewals keep failing, every held run is stopped
once two thirds of the lease have passed since the last renewal went out, before any lease can
run out and another worker take the run. Fencing still refuses the writes of an owner that
missed this, for instance because its process was suspended.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Coroutine, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.exc import SQLAlchemyError

log = logging.getLogger(__name__)


class Renewer(Protocol):
    """`swarmeval.control.queue.Queue` in production."""

    async def renew(self, owner_id: str, held: Mapping[str, int], lease_s: float) -> set[str]: ...


class LeaseLost(Exception):
    """This worker no longer holds the run's lease, so the run was stopped here."""


@dataclass
class _Held:
    owner_epoch: int
    task: asyncio.Future[Any]
    lost: str | None = None


class Leases:
    def __init__(self, queue: Renewer, owner_id: str, lease_s: float) -> None:
        self._queue = queue
        self._owner_id = owner_id
        self._lease_s = lease_s
        self._held: dict[str, _Held] = {}

    @property
    def lease_s(self) -> float:
        return self._lease_s

    @asynccontextmanager
    async def renewing(self) -> AsyncGenerator[None]:
        """Renews the held leases for the length of the block."""
        renewer = asyncio.create_task(self._renew())
        try:
            yield
        finally:
            renewer.cancel()
            await asyncio.wait([renewer])
            if not renewer.cancelled():
                renewer.result()

    async def hold[T](self, run_id: str, owner_epoch: int, work: Coroutine[Any, Any, T]) -> T:
        """Runs `work` on a run claimed at `owner_epoch`, renewing its lease meanwhile. Raises
        `LeaseLost`, once `work` is cancelled and done, if the lease is lost first. Only works
        inside `renewing`."""
        older = self._held.get(run_id)
        if older is not None:
            # This worker took over its own run, its lease having run out while it stalled.
            self._lose(run_id, older.owner_epoch, f"taken over again at owner_epoch {owner_epoch}")
        task = asyncio.ensure_future(work)
        held = self._held[run_id] = _Held(owner_epoch, task)
        try:
            return await task
        except asyncio.CancelledError as err:
            current = asyncio.current_task()
            if held.lost is None or (current is not None and current.cancelling()):
                raise
            raise LeaseLost(
                f"worker `{self._owner_id}` lost the lease of run `{run_id}` at owner_epoch "
                f"{owner_epoch}: {held.lost}. It stopped executing the run; the worker that "
                "takes the run over finishes it."
            ) from err
        finally:
            if self._held.get(run_id) is held:
                del self._held[run_id]

    async def _renew(self) -> None:
        clock = asyncio.get_running_loop()
        interval = self._lease_s / 3
        # When the last successful renewal went out. Every lease held now was set no earlier, so
        # it lasts at least `lease_s` from then.
        last_sent = clock.time()
        while True:
            await asyncio.sleep(interval)
            sent = clock.time()
            held = {run_id: h.owner_epoch for run_id, h in self._held.items()}
            try:
                async with asyncio.timeout(interval):
                    renewed = await self._queue.renew(self._owner_id, held, self._lease_s)
            except (SQLAlchemyError, TimeoutError):
                deadline = last_sent + self._lease_s - interval
                if clock.time() < deadline:
                    log.warning(
                        "worker %s could not renew its leases; trying again in %.1f s",
                        self._owner_id,
                        interval,
                        exc_info=True,
                    )
                    continue
                log.error(
                    "worker %s has not renewed its leases for %.1f s; stopping its %d runs "
                    "before their leases run out",
                    self._owner_id,
                    clock.time() - last_sent,
                    len(self._held),
                    exc_info=True,
                )
                reason = f"renewals failed for {clock.time() - last_sent:.1f} s"
                for run_id in list(self._held):
                    self._lose(run_id, self._held[run_id].owner_epoch, reason)
                # Nothing is held any more; a run claimed from now on has a lease of its own.
                last_sent = sent
                continue
            last_sent = sent
            for run_id in held.keys() - renewed:
                self._lose(run_id, held[run_id], "another worker took it over or finished it")

    def _lose(self, run_id: str, owner_epoch: int, reason: str) -> None:
        held = self._held.get(run_id)
        if held is None or held.owner_epoch != owner_epoch or held.lost is not None:
            return
        log.warning("worker %s: run %s lost its lease (%s)", self._owner_id, run_id, reason)
        held.lost = reason
        held.task.cancel()
