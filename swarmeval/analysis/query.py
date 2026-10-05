"""SQL over the exports: a DuckDB connection that sees the views `runs` and `events` and nothing
else (docs/services/analysis.md#queries).

pyarrow reads the bucket; DuckDB scans the Arrow datasets in process. Once the views are
registered the connection's own file and network access is switched off and its configuration
locked, so a statement can neither read another file nor switch access back on.
"""

import math
from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import cast

import duckdb
import pyarrow as pa
import pyarrow.dataset as ds
from pyarrow.fs import FileSelector, FileType
from pydantic import JsonValue

from swarmeval.events import EVENTS_SCHEMA, SUMMARY_SCHEMA, ObjectStore, events_key

MAX_ROWS = 10_000
TIMEOUT_S = 30.0
CHUNK_ROWS = 500
MEMORY_LIMIT = "1GB"

_EXACT_INT = 2**53
"""Integers up to here survive a JSON number; larger ones are sent as strings."""


class QueryError(Exception):
    """The caller's statement does not parse or run, or is not a single SELECT."""


class QueryTimeout(Exception):
    pass


def _files(store: ObjectStore, prefix: str, name: str | None = None) -> list[str]:
    """Paths of the Parquet files under `prefix`; only those called `name`, when given."""
    found = store.filesystem().get_file_info(
        FileSelector(f"{store.bucket}/{prefix}", recursive=True, allow_not_found=True)
    )
    return sorted(
        info.path
        for info in found
        if info.type == FileType.File
        and (info.base_name == name if name else info.base_name.endswith(".parquet"))
    )


def _dataset(store: ObjectStore, paths: Sequence[str], schema: pa.Schema) -> ds.Dataset:
    return ds.dataset(  # pyright: ignore[reportUnknownMemberType]
        list(paths), filesystem=store.filesystem(), format="parquet", schema=schema
    )


def connect(store: ObjectStore, run_ids: Sequence[str] | None = None) -> duckdb.DuckDBPyConnection:
    """A connection with `runs`, every run summary in the bucket, and `events`, the events of
    every exported run, or of `run_ids` only (those of them that are exported). Blocking. Close
    it when done."""
    if run_ids is None:
        event_files = _files(store, "runs", "events.parquet")
    else:
        exported = set(_files(store, "runs", "events.parquet"))
        wanted = (f"{store.bucket}/{events_key(run_id)}" for run_id in run_ids)
        event_files = sorted(path for path in wanted if path in exported)
    con = duckdb.connect(":memory:")
    con.register("runs", _dataset(store, _files(store, "summaries"), SUMMARY_SCHEMA))
    con.register("events", _dataset(store, event_files, EVENTS_SCHEMA))
    # Without this a statement could name a Python variable of this process as a table.
    con.execute("SET python_enable_replacements = false")
    con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


def start(
    con: duckdb.DuckDBPyConnection, sql: str
) -> tuple[list[tuple[str, str]], pa.RecordBatchReader]:
    """Runs `sql` and returns its columns as (name, DuckDB type) and a reader of its rows, in
    batches of `CHUNK_ROWS`, for `fetch`. Blocking. Raises `QueryError` for anything but one
    SELECT that runs, and `QueryTimeout` when the connection was interrupted meanwhile."""
    try:
        statements = con.extract_statements(sql)
    except duckdb.Error as err:
        raise QueryError(f"the statement does not parse: {err}") from err
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise QueryError(
            "give one SELECT statement. Queries are read-only over the views `runs` and "
            "`events`; nothing can be created, changed, or set."
        )
    try:
        con.execute(statements[0])
        columns = [(str(column[0]), str(column[1])) for column in con.description or []]
        # Arrow, not Python rows: DuckDB needs `pytz` to build a Python datetime with a zone.
        return columns, con.to_arrow_reader(CHUNK_ROWS)
    except duckdb.InterruptException as err:
        raise QueryTimeout from err
    except duckdb.Error as err:
        raise QueryError(str(err)) from err


def fetch(reader: pa.RecordBatchReader) -> list[list[JsonValue]] | None:
    """The next batch of rows as JSON values; `None` once there are no more. Blocking."""
    try:
        batch = reader.read_next_batch()
    except StopIteration:
        return None
    except duckdb.InterruptException as err:
        raise QueryTimeout from err
    except duckdb.Error as err:
        raise QueryError(str(err)) from err
    # By column, not `to_pylist()` on the batch: that makes dicts, which lose a repeated name.
    columns = [cast("list[object]", batch.column(i).to_pylist()) for i in range(batch.num_columns)]
    return [[plain(value) for value in row] for row in zip(*columns, strict=True)]


def plain(value: object) -> JsonValue:
    """A DuckDB value as JSON: what JSON numbers cannot hold exactly becomes a string."""
    match value:
        case None | bool() | str():
            return value
        case int():
            return value if abs(value) <= _EXACT_INT else str(value)
        case float():
            return value if math.isfinite(value) else str(value)
        case datetime() | date() | time():
            return value.isoformat()
        case bytes() | bytearray():
            return value.hex()
        case Decimal():
            return str(value)
        case list() | tuple():
            return [plain(item) for item in cast("Sequence[object]", value)]
        case dict():
            return {str(k): plain(v) for k, v in cast("dict[object, object]", value).items()}
        case _:
            return str(value)
