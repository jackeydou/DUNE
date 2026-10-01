"""Content-addressed blobs: full tool output and changed file contents (docs/event-log.md).

A blob is uploaded before the event that names it is committed, so a committed hash always
resolves. Keys are content addresses, so a retried upload is harmless.
"""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from swarmeval.events import ObjectStore


def blob_key(sha256: str) -> str:
    return f"blobs/sha256/{sha256}"


class BlobStore(Protocol):
    async def put(self, sha256: str, data: bytes) -> None:
        """Stores `data`, whose sha256 the caller has already checked, under its hash."""
        ...


@dataclass(frozen=True)
class S3BlobStore:
    store: ObjectStore

    async def put(self, sha256: str, data: bytes) -> None:
        await asyncio.to_thread(self.store.put, blob_key(sha256), data)
