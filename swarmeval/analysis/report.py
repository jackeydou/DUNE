"""Trigger rates per case, variant, and scorer over the epochs that finished, read from run
summaries (docs/services/analysis.md#reports).

Only `done` runs count. Runs that ended any other way are listed per status beside the rates.
An interrupted run is rerun at a new epoch by the control plane, so per variant the report also
says how many epochs were asked for, how many are `done`, and how many were rerun
(trajectory-first spec Q4).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import duckdb
import pyarrow as pa
import pyarrow.dataset as ds

from swarmeval.events import SUMMARY_SCHEMA, ObjectStore

Z95 = 1.959963984540054


@dataclass(frozen=True)
class Rate:
    case_id: str
    case_sha256: str
    """The case bundle's hash. Two revisions of a case are two experiments, rated apart."""
    variant: int
    task_args: str
    scorer: str
    epochs: int
    """`done` runs this scorer scored."""
    rate: float
    """Mean score; with `1 = triggered` scores, the share of epochs that triggered."""
    stderr: float | None
    """Of the mean. `None` with one epoch."""
    ci_low: float
    ci_high: float
    """95% Wilson interval. Unlike mean ± 1.96·stderr, it does not collapse to a point when no
    epoch or every epoch triggered, which is common with ten epochs."""


@dataclass(frozen=True)
class Unscored:
    case_id: str
    case_sha256: str
    variant: int
    task_args: str
    status: str
    runs: int


@dataclass(frozen=True)
class Coverage:
    case_id: str
    case_sha256: str
    variant: int
    task_args: str
    requested: int
    """Epochs the submissions asked for, summed over the submissions with a finished run."""
    done: int
    replaced: int
    """Interrupted runs that were rerun at a new epoch."""

    @property
    def missing(self) -> int:
        """Requested epochs with no `done` run: failed, cancelled, still running, or interrupted
        once the variant had used up its reruns."""
        return self.requested - self.done


@dataclass(frozen=True)
class Report:
    rates: tuple[Rate, ...]
    unscored: tuple[Unscored, ...]
    """Runs left out of the rates because they did not end `done`."""
    coverage: tuple[Coverage, ...]


def load_summaries(store: ObjectStore) -> pa.Table:
    """Every run summary in the bucket. Blocking."""
    dataset = ds.dataset(  # pyright: ignore[reportUnknownMemberType]
        f"{store.bucket}/summaries",
        filesystem=store.filesystem(),
        format="parquet",
        schema=SUMMARY_SCHEMA,
    )
    return dataset.to_table()


def report(
    summaries: pa.Table, submissions: Sequence[str] = (), suites: Sequence[str] = ()
) -> Report:
    """`submissions` and `suites` (suite labels) narrow the report to the runs of any of them;
    both empty means every run."""
    con = duckdb.connect()
    con.register("summaries", summaries)
    runs = """
        WITH runs AS (
            SELECT * FROM summaries
            WHERE $all OR list_contains($submissions, submission_id)
                  OR list_contains($suites, suite)
        )
    """
    params = {
        "all": not submissions and not suites,
        "submissions": list(submissions) or [""],
        "suites": list(suites) or [""],
    }
    rows = con.execute(
        runs
        + """
        SELECT case_id, case_sha256, variant, task_args, s.scorer, count(*), avg(s.value),
               stddev_samp(s.value) / sqrt(count(*))
        FROM (SELECT case_id, case_sha256, variant, task_args, unnest(scores) AS s FROM runs
              WHERE status = 'done')
        GROUP BY ALL
        ORDER BY ALL
        """,
        params,
    ).fetchall()
    rates = tuple(
        Rate(
            case_id=case_id,
            case_sha256=case_sha256,
            variant=variant,
            task_args=task_args,
            scorer=scorer,
            epochs=n,
            rate=rate,
            stderr=stderr,
            ci_low=wilson(rate, n)[0],
            ci_high=wilson(rate, n)[1],
        )
        for case_id, case_sha256, variant, task_args, scorer, n, rate, stderr in rows
    )
    unscored = tuple(
        Unscored(case_id=c, case_sha256=h, variant=v, task_args=t, status=s, runs=n)
        for c, h, v, t, s, n in con.execute(
            runs
            + """
            SELECT case_id, case_sha256, variant, task_args, status, count(*) FROM runs
            WHERE status <> 'done' GROUP BY ALL ORDER BY ALL
            """,
            params,
        ).fetchall()
    )
    coverage = tuple(
        Coverage(case_id=c, case_sha256=h, variant=v, task_args=t, requested=r, done=d, replaced=x)
        for c, h, v, t, r, d, x in con.execute(
            runs
            + """
            , requested AS (
                SELECT case_id, case_sha256, variant, task_args, sum(epochs) AS requested
                FROM (SELECT DISTINCT submission_id, case_id, case_sha256, variant, task_args,
                             epochs FROM runs)
                GROUP BY ALL
            ), counts AS (
                SELECT case_id, case_sha256, variant, task_args,
                       count(*) FILTER (WHERE status = 'done') AS done,
                       count(replaced_by) AS replaced
                FROM runs GROUP BY ALL
            )
            SELECT case_id, case_sha256, variant, task_args, requested, done, replaced
            FROM requested JOIN counts USING (case_id, case_sha256, variant, task_args)
            ORDER BY ALL
            """,
            params,
        ).fetchall()
    )
    return Report(rates=rates, unscored=unscored, coverage=coverage)


def wilson(rate: float, n: int, z: float = Z95) -> tuple[float, float]:
    centre = (rate + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def markdown(result: Report) -> str:
    lines = [
        "| case | variant | task_args | scorer | epochs | rate | stderr | 95% CI |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in result.rates:
        stderr = "-" if r.stderr is None else f"{r.stderr:.3f}"
        lines.append(
            f"| {r.case_id}@{r.case_sha256[:8]} | {r.variant} | `{r.task_args}` | {r.scorer} | "
            f"{r.epochs} | "
            f"{r.rate:.3f} | {stderr} | [{r.ci_low:.3f}, {r.ci_high:.3f}] |"
        )
    lines += [
        "",
        "Epochs per variant (an interrupted run is rerun at a new epoch, at most `epochs` times):",
        "",
        "| case | variant | task_args | requested | done | replaced | missing |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {c.case_id}@{c.case_sha256[:8]} | {c.variant} | `{c.task_args}` | {c.requested} | "
        f"{c.done} | {c.replaced} | {c.missing} |"
        for c in result.coverage
    ]
    if result.unscored:
        lines += ["", "Not counted (did not end `done`):", ""]
        lines += [
            f"- {u.case_id}@{u.case_sha256[:8]} variant {u.variant} `{u.task_args}`: "
            f"{u.runs} {u.status}"
            for u in result.unscored
        ]
    return "\n".join(lines) + "\n"
