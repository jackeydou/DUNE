from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pytest

from swarmeval.analysis import report
from swarmeval.analysis.report import markdown, wilson
from swarmeval.events import SUMMARY_SCHEMA

T = datetime(2026, 10, 2, tzinfo=UTC)


def summary(
    run: str,
    *,
    variant: int = 0,
    status: str = "done",
    submission: str = "sub1",
    scores: dict[str, float] | None = None,
    case_sha256: str = "ab" * 32,
) -> dict[str, Any]:
    return {
        "run_id": run,
        "submission_id": submission,
        "case_id": "scorer_misbelief",
        "case_sha256": case_sha256,
        "workspace": "ws",
        "variant": variant,
        "task_args": f'{{"framing": "v{variant}"}}',
        "epoch": 1,
        "epochs": 3,
        "status": status,
        "error": None,
        "isolation": "runc",
        "started_at": T,
        "finished_at": T,
        "scores": [
            {"scorer": k, "value": v, "explanation": None} for k, v in (scores or {}).items()
        ],
    }


def table(*rows: dict[str, Any]) -> pa.Table:
    return pa.Table.from_pylist(list(rows), schema=SUMMARY_SCHEMA)


def test_rates_are_per_variant_and_scorer_over_done_runs() -> None:
    summaries = table(
        summary("r1", scores={"tampered": 1, "read_key": 0}),
        summary("r2", scores={"tampered": 0, "read_key": 0}),
        summary("r3", scores={"tampered": 1, "read_key": 0}),
        summary("r4", variant=1, scores={"tampered": 0}),
        summary("r5", variant=1, status="failed"),
        summary("r6", variant=1, status="interrupted", scores={"tampered": 1}),
    )

    result = report(summaries)

    by = {(r.variant, r.scorer): r for r in result.rates}
    assert set(by) == {(0, "read_key"), (0, "tampered"), (1, "tampered")}
    tampered = by[(0, "tampered")]
    assert (tampered.epochs, tampered.rate) == (3, pytest.approx(2 / 3))
    assert tampered.stderr == pytest.approx(0.3333, abs=1e-4)
    assert (tampered.ci_low, tampered.ci_high) == pytest.approx((0.2077, 0.9385), abs=1e-4)
    assert by[(0, "read_key")].rate == 0
    assert by[(0, "read_key")].ci_low == pytest.approx(0, abs=1e-12)
    assert by[(1, "tampered")].epochs == 1 and by[(1, "tampered")].stderr is None
    assert [(u.variant, u.status, u.runs) for u in result.unscored] == [
        (1, "failed", 1),
        (1, "interrupted", 1),
    ]


def test_a_report_can_be_narrowed_to_submissions() -> None:
    summaries = table(
        summary("r1", submission="sub1", scores={"tampered": 1}),
        summary("r2", submission="sub2", scores={"tampered": 0}),
    )

    result = report(summaries, ["sub2"])

    assert [(r.scorer, r.epochs, r.rate) for r in result.rates] == [("tampered", 1, 0.0)]


def test_the_wilson_interval_does_not_collapse_at_zero_or_one() -> None:
    assert wilson(0.0, 10) == pytest.approx((0.0, 0.2775), abs=1e-4)
    assert wilson(1.0, 10) == pytest.approx((0.7225, 1.0), abs=1e-4)


def test_markdown_lists_rates_and_runs_left_out() -> None:
    result = report(table(summary("r1", scores={"tampered": 1}), summary("r2", status="cancelled")))

    text = markdown(result)

    assert (
        '| scorer_misbelief@abababab | 0 | `{"framing": "v0"}` | tampered | 1 | 1.000 | - |' in text
    )
    assert '- scorer_misbelief@abababab variant 0 `{"framing": "v0"}`: 1 cancelled' in text


def test_revisions_of_a_case_are_rated_apart() -> None:
    summaries = table(
        summary("r1", scores={"tampered": 1}),
        summary("r2", scores={"tampered": 0}, case_sha256="cd" * 32),
    )

    result = report(summaries)

    assert sorted((r.case_sha256[:2], r.epochs, r.rate) for r in result.rates) == [
        ("ab", 1, 1.0),
        ("cd", 1, 0.0),
    ]
