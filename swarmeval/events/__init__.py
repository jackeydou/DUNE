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
    blob_key,
    export_key,
    export_run,
    load_run,
    write_eval,
)
from swarmeval.events.parquet import (
    EVENTS_SCHEMA,
    SUMMARY_SCHEMA,
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
from swarmeval.events.variant import (
    DEFAULT_REDUCERS,
    LeftOut,
    VariantLogError,
    read_eval,
    variant_log,
)

__all__ = [
    "DEFAULT_REDUCERS",
    "EVENTS_SCHEMA",
    "NOTIFY_CHANNEL",
    "SCHEMA_VERSION",
    "SUMMARY_SCHEMA",
    "ChainError",
    "ChainRow",
    "FencedError",
    "LeftOut",
    "ObjectStore",
    "PostgresRunStore",
    "RunHeader",
    "RunNotFoundError",
    "StoredRun",
    "VariantLogError",
    "assemble",
    "blob_key",
    "events_key",
    "export_events",
    "export_key",
    "export_run",
    "export_summary",
    "genesis",
    "link",
    "load_run",
    "read_eval",
    "summary_key",
    "to_event",
    "to_inspect_message",
    "variant_log",
    "verify",
    "write_eval",
]
