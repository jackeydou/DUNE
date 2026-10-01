"""Wakeups for live event streams: one `LISTEN` connection per control-plane replica.

A notification only says "run X committed something"; subscribers then read `runs.events`. A
lost notification costs latency, never an event, because readers also poll.
"""

import asyncio
from collections.abc import Generator
from contextlib import contextmanager

import psycopg

from swarmeval.events import NOTIFY_CHANNEL


class EventListener:
    def __init__(self, database_url: str) -> None:
        """`database_url` is a libpq URL (`postgresql://...`)."""
        self._url = database_url
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self._conn: psycopg.AsyncConnection[tuple[object, ...]] | None = None
        self._pump: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._conn = await psycopg.AsyncConnection.connect(self._url, autocommit=True)
        await self._conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
        self._pump = asyncio.create_task(self._run(self._conn))

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            await asyncio.gather(self._pump, return_exceptions=True)
        if self._conn is not None:
            await self._conn.close()

    @contextmanager
    def subscribe(self, run_id: str) -> Generator[asyncio.Event]:
        """An event set whenever `run_id` commits. The caller clears it before each read."""
        waiter = asyncio.Event()
        self._waiters.setdefault(run_id, set()).add(waiter)
        try:
            yield waiter
        finally:
            waiters = self._waiters[run_id]
            waiters.discard(waiter)
            if not waiters:
                del self._waiters[run_id]

    async def _run(self, conn: psycopg.AsyncConnection[tuple[object, ...]]) -> None:
        async for notify in conn.notifies():
            for waiter in self._waiters.get(notify.payload, ()):
                waiter.set()
