"""The `eval-set` job: one Inspect `.eval` per variant, from the per-run logs of its epochs
(docs/services/analysis.md#capabilities).

Variants are grouped by submission, case revision (`case_sha256`), and variant index, read from
the run summaries. Only `done` runs become samples; the rest are listed in the log's metadata
and left out, as the report leaves them out (runtime spec Q6). Logs go to a local directory:
analysis never writes object storage.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa

from swarmeval.events import (
    DEFAULT_REDUCERS,
    LeftOut,
    ObjectStore,
    export_key,
    read_eval,
    variant_log,
    write_eval,
)


class EvalSetError(Exception):
    """A run the summaries call `done` has no log in the bucket."""


@dataclass(frozen=True)
class VariantRuns:
    submission_id: str
    case_id: str
    case_sha256: str
    variant: int
    task_args: str
    done: tuple[str, ...]
    """Run ids, in epoch order."""
    left_out: tuple[LeftOut, ...]

    @property
    def filename(self) -> str:
        return f"{self.submission_id}_{self.case_id}-{self.case_sha256[:8]}_v{self.variant}.eval"


@dataclass(frozen=True)
class Written:
    variant: VariantRuns
    path: Path | None
    """`None` when the variant has no `done` run, so there is no log to write."""


def variant_runs(summaries: pa.Table, submissions: Sequence[str] = ()) -> list[VariantRuns]:
    """`submissions` narrows to those submissions; empty means every run in the summaries."""
    con = duckdb.connect()
    con.register("summaries", summaries)
    rows = con.execute(
        """
        SELECT submission_id, case_id, case_sha256, variant, task_args,
               list(run_id ORDER BY epoch, run_id) FILTER (WHERE status = 'done'),
               list({'run_id': run_id, 'epoch': epoch, 'status': status}
                    ORDER BY epoch, run_id) FILTER (WHERE status <> 'done')
        FROM summaries
        WHERE $all OR list_contains($submissions, submission_id)
        GROUP BY ALL
        ORDER BY ALL
        """,
        {"all": not submissions, "submissions": list(submissions) or [""]},
    ).fetchall()
    return [
        VariantRuns(
            submission_id=submission_id,
            case_id=case_id,
            case_sha256=case_sha256,
            variant=variant,
            task_args=task_args,
            done=tuple(done or ()),
            left_out=tuple(LeftOut(**r) for r in left or ()),
        )
        for submission_id, case_id, case_sha256, variant, task_args, done, left in rows
    ]


async def write_eval_set(
    store: ObjectStore,
    variants: Sequence[VariantRuns],
    out_dir: Path,
    reducers: Sequence[str] = DEFAULT_REDUCERS,
) -> list[Written]:
    """Writes one log per variant with a `done` run into `out_dir`, replacing a log of the same
    name, so running the job again gives the same files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Written] = []
    for variant in variants:
        if not variant.done:
            written.append(Written(variant, None))
            continue
        logs = [read_eval(await _get(store, run_id)) for run_id in variant.done]
        log = variant_log(
            logs,
            submission_id=variant.submission_id,
            case_sha256=variant.case_sha256,
            reducers=reducers,
            left_out=variant.left_out,
        )
        path = out_dir / variant.filename
        await asyncio.to_thread(write_eval, log, path)
        written.append(Written(variant, path))
    return written


async def _get(store: ObjectStore, run_id: str) -> bytes:
    try:
        return await asyncio.to_thread(store.get, export_key(run_id))
    except FileNotFoundError as err:
        raise EvalSetError(
            f"run {run_id} is `done` in its summary, but `{export_key(run_id)}` is not in "
            f"bucket {store.bucket}. Check the bucket and the worker's export of that run."
        ) from err


def describe(written: Sequence[Written]) -> str:
    """One line per variant: where its log went and which runs it left out."""
    lines: list[str] = []
    for w in written:
        v = w.variant
        name = f"{v.submission_id} {v.case_id}@{v.case_sha256[:8]} variant {v.variant}"
        left = ", ".join(f"{r.run_id} (epoch {r.epoch}, {r.status})" for r in v.left_out)
        where = f"{w.path}: {len(v.done)} epochs" if w.path else "no `done` run, not written"
        lines.append(f"{name}\t{where}" + (f"; left out: {left}" if left else ""))
    return "\n".join(lines) + "\n"
