"""`report --compare`: the difference in rate between two values of one variant axis."""

import json
from typing import Any

import pytest

from swarmeval.analysis.compare import (
    CompareError,
    Comparison,
    compare,
    markdown,
    newcombe,
    parse_comparison,
)
from swarmeval.analysis.report import report
from tests.analysis.test_report import summary, table


def run(name: str, paraphrased: list[str], value: float, **args: Any) -> dict[str, Any]:
    row = summary(name, scores={"coordinated": value})
    row["task_args"] = json.dumps({"paraphrased": paraphrased, **args}, sort_keys=True)
    row["variant"] = 1 if paraphrased else 0
    return row


def test_newcombe_matches_the_published_example() -> None:
    # Newcombe (1998), table II example (b): 56/70 against 48/80, method 10.
    low, high = newcombe(48 / 80, 80, 56 / 70, 70)

    assert (round(low, 4), round(high, 4)) == (0.0524, 0.3339)


def test_newcombe_stays_inside_minus_one_to_one_at_the_extremes() -> None:
    low, high = newcombe(0.0, 10, 1.0, 10)

    assert 0 < low < 1 and high == 1.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("paraphrased=[],[dm_ab]", Comparison("paraphrased", [], ["dm_ab"])),
        ("temperature=0,0.6", Comparison("temperature", 0, 0.6)),
        ("framing=neutral, pressure", Comparison("framing", "neutral", "pressure")),
        ("flag=true,false", Comparison("flag", True, False)),
    ],
)
def test_comparisons_parse_values_with_their_types(text: str, expected: Comparison) -> None:
    assert parse_comparison(text) == expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[],[dm_ab]", "names no axis"),
        ("paraphrased=[]", "give exactly two values"),
        ("paraphrased=a,b,c", "give exactly two values"),
        ("paraphrased=[],[]", "same value twice"),
        ("paraphrased=[,", "do not parse"),
    ],
)
def test_bad_comparisons_are_refused(text: str, message: str) -> None:
    with pytest.raises(CompareError, match=message):
        parse_comparison(text)


def test_pairs_hold_the_other_axes_equal() -> None:
    summaries = table(
        run("v1", [], 1, model="m1"),
        run("v2", [], 1, model="m1"),
        run("p1", ["dm_ab"], 0, model="m1"),
        run("p2", ["dm_ab"], 1, model="m1"),
        run("v3", [], 0, model="m2"),
        run("p3", ["dm_ab"], 0, model="m2"),
        run("only", [], 1, model="m3"),
    )

    differences = compare(report(summaries), parse_comparison("paraphrased=[],[dm_ab]"))

    assert [(d.others, d.a.epochs, d.b.epochs, d.diff) for d in differences] == [
        ('{"model": "m1"}', 2, 2, -0.5),
        ('{"model": "m2"}', 1, 1, 0.0),
    ]
    first = differences[0]
    assert (first.ci_low, first.ci_high) == pytest.approx(newcombe(1.0, 2, 0.5, 2))
    text = markdown(differences, parse_comparison("paraphrased=[],[dm_ab]"))
    assert '| `{"model": "m1"}` | coordinated | 2 | 1.000 | 2 | 0.500 | -0.500 |' in text


def test_an_axis_no_case_has_gives_no_pairs() -> None:
    differences = compare(report(table(run("v1", [], 1))), parse_comparison("model=a,b"))

    assert differences == ()
    assert "No case revision has `done` runs at both values" in markdown(
        differences, parse_comparison("model=a,b")
    )
