"""The run's single writer: commits transactions in order and fans committed events out."""

import asyncio
from collections.abc import Callable, Sequence

from swarmeval.runtime.ports import RunStore
from swarmeval.runtime.records import CommittedEvent, Transaction

Subscriber = Callable[[Sequence[CommittedEvent]], None]


class RunWriter:
    """Every commit of a run goes through one instance of this class.

    Commits are serialized, and subscribers see committed events in `seq` order. Subscribers
    are called synchronously while the commit lock is held, so they must not block.
    """

    def __init__(self, store: RunStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()
        self._subscribers: list[Subscriber] = []
        self._last_event_id: str | None = None

    @property
    def store(self) -> RunStore:
        return self._store

    @property
    def last_event_id(self) -> str | None:
        """The last event committed through this writer, `None` before the first. Run-level
        events that follow a phase of the run (the self-check, the loop) take it as parent."""
        return self._last_event_id

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.remove(subscriber)

    async def commit(self, txn: Transaction) -> list[CommittedEvent]:
        if txn.is_empty():
            return []
        async with self._lock:
            events = await self._store.commit(txn)
            if events:
                self._last_event_id = events[-1].event_id
                for subscriber in self._subscribers:
                    subscriber(events)
            return events
