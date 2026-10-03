import io
import secrets
from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from swarmeval.analysis import load_summaries
from swarmeval.events import SUMMARY_SCHEMA, ObjectStore, summary_key

pytestmark = pytest.mark.docker


def test_summaries_written_before_a_column_existed_read_it_as_null(
    object_store: ObjectStore,
) -> None:
    added = {"replaces", "replaced_by", "suite"}
    run_id = f"old.{secrets.token_hex(4)}.v0.e1"
    t = datetime(2026, 10, 1, tzinfo=UTC)
    row: dict[str, object] = {
        "run_id": run_id,
        "submission_id": "old",
        "case_id": "c",
        "case_sha256": "ab" * 32,
        "workspace": "ws",
        "variant": 0,
        "task_args": "{}",
        "epoch": 1,
        "epochs": 1,
        "status": "done",
        "error": None,
        "isolation": "runc",
        "started_at": t,
        "finished_at": t,
        "scores": [],
    }
    out = io.BytesIO()
    older = pa.Table.from_pylist([row], schema=SUMMARY_SCHEMA).drop_columns(sorted(added))
    pq.write_table(older, out)  # pyright: ignore[reportUnknownMemberType]
    object_store.put(summary_key(run_id), out.getvalue())

    (read,) = [r for r in load_summaries(object_store).to_pylist() if r["run_id"] == run_id]

    assert {k: read[k] for k in added} == dict.fromkeys(added)
    assert read["status"] == "done"
