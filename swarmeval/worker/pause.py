"""Pausing a run for a person (M2 spec decision 7): the run's row says `paused` until
`ResumeRun` sets it back to `running`, or a cancel ends it."""

import asyncio
import logging

from swarmeval.control.queue import Queue

log = logging.getLogger(__name__)


class QueuePauser:
    """The production `Pauser`. Polls the run's status while paused, every `poll_s`."""

    def __init__(self, queue: Queue, run_id: str, owner_epoch: int, poll_s: float) -> None:
        self._queue = queue
        self._run_id = run_id
        self._owner_epoch = owner_epoch
        self._poll_s = poll_s

    async def wait(self, reason: str) -> None:
        status = await self._queue.pause(self._run_id, self._owner_epoch)
        if status == "paused":
            log.warning(
                "run %s paused: %s. Resume it with ResumeRun, or cancel it.", self._run_id, reason
            )
        while status == "paused":
            await asyncio.sleep(self._poll_s)
            status = await self._queue.status(self._run_id)
        log.info("run %s: pause over, now %s", self._run_id, status)
