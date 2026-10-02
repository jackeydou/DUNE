"""Trigger rates per case, variant, and scorer over the epochs that finished, read from run
summaries (docs/services/analysis.md#reports).

Only `done` runs count. Runs that ended any other way are listed per status beside the rates,
since how interrupted and re-run epochs should count is still open (runtime spec Q6).
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
    variant: int
    task_args: str
    status: str
    runs: int


@dataclass(frozen=True)
class Report:
    rates: tuple[Rate, ...]
    unscored: tuple[Unscored, ...]
    """Runs left out of the rates because they did not end `done`."""


def load_summaries(store: ObjectStore) -> pa.Table:
    """Every run summary in the bucket. Blocking."""
    dataset = ds.dataset(  # pyright: ignore[reportUnknownMemberType]
        f"{store.bucket}/summaries",
        filesystem=store.filesystem(),
        format="parquet",
        schema=SUMMARY_SCHEMA,
    )
    return dataset.to_table()


def report(summaries: pa.Table, submissions: Sequence[str] = ()) -> Report:
    """`submissions` narrows the report to those submissions; empty means every run."""
    con = duckdb.connect()
    con.register("summaries", summaries)
    runs = """
        WITH runs AS (
            SELECT * FROM summaries WHERE $all OR list_contains($submissions, submission_id)
        )
    """
    params = {"all": not submissions, "submissions": list(submissions) or [""]}
    rows = con.execute(
        runs
        + """
        SELECT case_id, variant, task_args, s.scorer, count(*), avg(s.value),
               stddev_samp(s.value) / sqrt(count(*))
        FROM (SELECT case_id, variant, task_args, unnest(scores) AS s FROM runs
              WHERE status = 'done')
        GROUP BY ALL
        ORDER BY ALL
        """,
        params,
    ).fetchall()
    rates = tuple(
        Rate(
            case_id=case_id,
            variant=variant,
            task_args=task_args,
            scorer=scorer,
            epochs=n,
            rate=rate,
            stderr=stderr,
            ci_low=wilson(rate, n)[0],
            ci_high=wilson(rate, n)[1],
        )
        for case_id, variant, task_args, scorer, n, rate, stderr in rows
    )
    unscored = tuple(
        Unscored(case_id=c, variant=v, task_args=t, status=s, runs=n)
        for c, v, t, s, n in con.execute(
            runs
            + """
            SELECT case_id, variant, task_args, status, count(*) FROM runs
            WHERE status <> 'done' GROUP BY ALL ORDER BY ALL
            """,
            params,
        ).fetchall()
    )
    return Report(rates=rates, unscored=unscored)


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
            f"| {r.case_id} | {r.variant} | `{r.task_args}` | {r.scorer} | {r.epochs} | "
            f"{r.rate:.3f} | {stderr} | [{r.ci_low:.3f}, {r.ci_high:.3f}] |"
        )
    if result.unscored:
        lines += ["", "Not counted (did not end `done`):", ""]
        lines += [
            f"- {u.case_id} variant {u.variant} `{u.task_args}`: {u.runs} {u.status}"
            for u in result.unscored
        ]
    return "\n".join(lines) + "\n"
