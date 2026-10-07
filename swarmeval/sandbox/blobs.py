"""Content-addressed blobs: full tool output and changed file contents (docs/event-log.md).

A blob is uploaded before the event that names it is committed, so a committed hash always
resolves. Keys are content addresses, so a retried upload is harmless.
"""

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Protocol

from swarmeval.events import ObjectStore, blob_key


class BlobStore(Protocol):
    async def put(self, sha256: str, data: bytes) -> None:
        """Stores `data`, whose sha256 the caller has already checked, under its hash."""
        ...


@dataclass(frozen=True)
class S3BlobStore:
    store: ObjectStore

    async def put(self, sha256: str, data: bytes) -> None:
        await asyncio.to_thread(self.store.put, blob_key(sha256), data)

    async def get(self, sha256: str) -> bytes:
        """The blob, checked against its hash. Raises `FileNotFoundError` when it was never
        stored."""
        data = await asyncio.to_thread(self.store.get, blob_key(sha256))
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            raise ValueError(
                f"blob {blob_key(sha256)} holds content hashing to {actual}. The object store "
                "was changed after upload; do not trust it."
            )
        return data
