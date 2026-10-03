"""Conditional differences: one variant axis at two values, every other axis held equal
(docs/services/analysis.md#reports, M2 spec decision 5).

For each case revision, scorer, and setting of the other axes that has `done` runs at both
values, the difference in trigger rate `B - A` with a 95% interval by Newcombe's hybrid score
method, built from the two Wilson intervals the single-group rates already use.
"""

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

import yaml
from pydantic import JsonValue

from swarmeval.analysis.report import Rate, Report, wilson


class CompareError(Exception):
    """A `--compare` argument does not name one axis and two values."""


@dataclass(frozen=True)
class Comparison:
    axis: str
    a: JsonValue
    b: JsonValue


@dataclass(frozen=True)
class Difference:
    case_id: str
    case_sha256: str
    scorer: str
    others: str
    """The other axes' values, as JSON text with sorted keys."""
    a: Rate
    b: Rate
    diff: float
    """`b.rate - a.rate`."""
    ci_low: float
    ci_high: float


def parse_comparison(text: str) -> Comparison:
    """`AXIS=A,B`. The values are read as a YAML flow sequence, so `[]`, `[dm_ab]`, numbers,
    and booleans keep their types: `paraphrased=[],[dm_ab]`, `temperature=0,0.6`."""
    axis, sep, values = text.partition("=")
    if not sep or not axis.strip():
        raise CompareError(
            f"--compare {text!r} names no axis. Write AXIS=A,B, like `paraphrased=[],[dm_ab]`."
        )
    try:
        parsed: object = yaml.safe_load(f"[{values}]")
    except yaml.YAMLError as err:
        raise CompareError(
            f"--compare {text!r}: the values {values!r} do not parse as A,B: {err}"
        ) from err
    items: list[object] = parsed if isinstance(parsed, list) else []  # pyright: ignore[reportUnknownVariableType]
    if len(items) != 2:
        raise CompareError(
            f"--compare {text!r} gives {values!r}; give exactly two values, A,B. Quote a "
            "string that holds a comma."
        )
    comparison = Comparison(axis=axis.strip(), a=_json(items[0]), b=_json(items[1]))
    if _canonical(comparison.a) == _canonical(comparison.b):
        raise CompareError(f"--compare {text!r} gives the same value twice. Give two values.")
    return comparison


def compare(result: Report, comparison: Comparison) -> tuple[Difference, ...]:
    """Pairs the report's rates. A setting with runs at only one of the two values is left out;
    a case without the axis has no pairs."""
    a_key, b_key = _canonical(comparison.a), _canonical(comparison.b)
    groups: dict[tuple[str, str, str, str], dict[str, Rate]] = {}
    for rate in result.rates:
        args: dict[str, JsonValue] = json.loads(rate.task_args)
        if comparison.axis not in args:
            continue
        value = _canonical(args.pop(comparison.axis))
        if value not in (a_key, b_key):
            continue
        others = json.dumps(args, sort_keys=True, ensure_ascii=False)
        key = (rate.case_id, rate.case_sha256, rate.scorer, others)
        groups.setdefault(key, {})["a" if value == a_key else "b"] = rate
    differences: list[Difference] = []
    for (case_id, case_sha256, scorer, others), pair in sorted(groups.items()):
        if len(pair) < 2:
            continue
        a, b = pair["a"], pair["b"]
        low, high = newcombe(a.rate, a.epochs, b.rate, b.epochs)
        differences.append(
            Difference(
                case_id=case_id,
                case_sha256=case_sha256,
                scorer=scorer,
                others=others,
                a=a,
                b=b,
                diff=b.rate - a.rate,
                ci_low=low,
                ci_high=high,
            )
        )
    return tuple(differences)


def newcombe(p_a: float, n_a: int, p_b: float, n_b: int) -> tuple[float, float]:
    """95% interval for `p_b - p_a` (Newcombe 1998, method 10). Like the Wilson interval it is
    built from, it stays informative when a rate is 0 or 1."""
    l_a, u_a = wilson(p_a, n_a)
    l_b, u_b = wilson(p_b, n_b)
    diff = p_b - p_a
    low = diff - math.sqrt((p_b - l_b) ** 2 + (u_a - p_a) ** 2)
    high = diff + math.sqrt((u_b - p_b) ** 2 + (p_a - l_a) ** 2)
    return max(-1.0, low), min(1.0, high)


def markdown(differences: Sequence[Difference], comparison: Comparison) -> str:
    a, b = _canonical(comparison.a), _canonical(comparison.b)
    lines = [
        f"Difference in rate, `{comparison.axis}` = `{b}` (B) minus `{a}` (A):",
        "",
        "| case | other axes | scorer | A epochs | A rate | B epochs | B rate | B - A | 95% CI |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {d.case_id}@{d.case_sha256[:8]} | `{d.others}` | {d.scorer} | {d.a.epochs} | "
        f"{d.a.rate:.3f} | {d.b.epochs} | {d.b.rate:.3f} | {d.diff:+.3f} | "
        f"[{d.ci_low:+.3f}, {d.ci_high:+.3f}] |"
        for d in differences
    ]
    if not differences:
        lines += ["", "No case revision has `done` runs at both values with the other axes equal."]
    return "\n".join(lines) + "\n"


def _json(value: object) -> JsonValue:
    """YAML gives JSON-compatible values for a flow sequence of scalars and lists."""
    return json.loads(json.dumps(value))


def _canonical(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)
