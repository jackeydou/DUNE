"""The event log: records as Inspect events, the hash chain, and the Postgres run store.

The only package that imports `inspect_ai` (docs/event-log.md).
"""

from swarmeval.events.chain import ChainError, ChainRow, genesis, link, verify
from swarmeval.events.convert import SCHEMA_VERSION, to_event, to_inspect_message
from swarmeval.events.export import (
    ObjectStore,
    RunHeader,
    StoredRun,
    assemble,
    export_key,
    export_run,
    load_run,
    write_eval,
)
from swarmeval.events.parquet import (
    EVENTS_SCHEMA,
    SUMMARY_SCHEMA,
    Finished,
    events_key,
    export_events,
    export_summary,
    summary_key,
)
from swarmeval.events.store import (
    NOTIFY_CHANNEL,
    FencedError,
    PostgresRunStore,
    RunNotFoundError,
)

__all__ = [
    "EVENTS_SCHEMA",
    "NOTIFY_CHANNEL",
    "SCHEMA_VERSION",
    "SUMMARY_SCHEMA",
    "ChainError",
    "ChainRow",
    "FencedError",
    "Finished",
    "ObjectStore",
    "PostgresRunStore",
    "RunHeader",
    "RunNotFoundError",
    "StoredRun",
    "assemble",
    "events_key",
    "export_events",
    "export_key",
    "export_run",
    "export_summary",
    "genesis",
    "link",
    "load_run",
    "summary_key",
    "to_event",
    "to_inspect_message",
    "verify",
    "write_eval",
]
