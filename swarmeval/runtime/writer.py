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

    @property
    def store(self) -> RunStore:
        return self._store

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
                for subscriber in self._subscribers:
                    subscriber(events)
            return events
