"""The event log: records as Inspect events, the hash chain, and the Postgres run store.

The only package that imports `inspect_ai` (docs/event-log.md).
"""

from swarmeval.events.chain import ChainError, ChainRow, genesis, link, verify
from swarmeval.events.convert import SCHEMA_VERSION, to_event, to_inspect_message
from swarmeval.events.store import (
    NOTIFY_CHANNEL,
    FencedError,
    PostgresRunStore,
    RunNotFoundError,
)

__all__ = [
    "NOTIFY_CHANNEL",
    "SCHEMA_VERSION",
    "ChainError",
    "ChainRow",
    "FencedError",
    "PostgresRunStore",
    "RunNotFoundError",
    "genesis",
    "link",
    "to_event",
    "to_inspect_message",
    "verify",
]
