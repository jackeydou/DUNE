"""Reading a run's exports from the bucket (docs/services/analysis.md#inputs)."""

import asyncio
import io
from collections.abc import Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from swarmeval.events import ObjectStore, events_key


class ExportError(Exception):
    """A run has no export in the bucket, or a selection of runs matched none."""


async def load_events_table(store: ObjectStore, run_id: str) -> pa.Table:
    """The run's `events.parquet`, in `seq` order."""
    return await asyncio.to_thread(read_events_table, store, run_id)


def read_events_table(store: ObjectStore, run_id: str) -> pa.Table:
    """`load_events_table`, blocking."""
    try:
        data = store.get(events_key(run_id))
    except FileNotFoundError as err:
        raise ExportError(
            f"run {run_id} has no `{events_key(run_id)}` in the bucket. Only runs that ended "
            "`done` or `cancelled` are exported; check the run's status."
        ) from err
    return pq.read_table(io.BytesIO(data))  # pyright: ignore[reportUnknownMemberType]


async def load_events(store: ObjectStore, run_id: str) -> list[dict[str, object]]:
    """The run's events as rows, in `seq` order."""
    return (await load_events_table(store, run_id)).to_pylist()


def runs_of(summaries: pa.Table, submissions: Sequence[str], statuses: Sequence[str]) -> list[str]:
    """Run ids of the submissions whose summary has one of `statuses`, sorted. Raises
    `ExportError` when none does."""
    run_ids = sorted(
        r["run_id"]
        for r in summaries.select(["run_id", "submission_id", "status"]).to_pylist()
        if r["submission_id"] in submissions and r["status"] in statuses
    )
    if not run_ids:
        raise ExportError(
            f"submissions {', '.join(submissions)} have no runs that ended "
            f"{' or '.join(f'`{s}`' for s in statuses)}."
        )
    return run_ids
