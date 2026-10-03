from pathlib import Path
from typing import Any

import pytest
import yaml

from swarmeval.core import SuiteError, load_suite
from tests.core.test_loader import base_case, write

REPO = Path(__file__).resolve().parents[2]


def model_case() -> dict[str, Any]:
    case = base_case()
    case["variants"] = {"model": ["m1"], "framing": ["a", "b"]}
    case["epochs"] = 3
    for agent in case["swarm"]["agents"]:
        agent["model"] = "${variant.model}"
    return case


def write_suite(root: Path, suite: dict[str, Any]) -> Path:
    path = root / "suites" / "s.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(suite, sort_keys=False))
    return path


def suite_error(root: Path, suite: dict[str, Any]) -> str:
    with pytest.raises(SuiteError) as err:
        load_suite(write_suite(root, suite))
    return str(err.value)


def test_the_m1_core_suite_loads() -> None:
    suite = load_suite(REPO / "suites" / "m1_core.yaml")

    assert suite.id == "m1_core"
    assert [e.case.id for e in suite.entries] == ["scorer_misbelief"]
    (entry,) = suite.entries
    assert len(entry.overrides["model"]) >= 3
    assert entry.epochs == 10


def test_models_fill_each_cases_model_axis_and_epochs_fall_back_in_order(
    tmp_path: Path,
) -> None:
    write(tmp_path, model_case())
    path = write_suite(
        tmp_path,
        {
            "schema_version": 1,
            "id": "core",
            "models": ["m1", "m2", "m3"],
            "epochs": 10,
            "cases": [
                {"path": "../case"},
                {"path": "../case", "variants": {"framing": ["b"]}, "epochs": 2},
            ],
        },
    )

    suite = load_suite(path)

    first, second = suite.entries
    assert first.dir == (tmp_path / "case").resolve()
    assert (first.overrides, first.epochs, first.runs) == (
        {"model": ["m1", "m2", "m3"]},
        10,
        60,
    )
    assert (second.overrides, second.epochs, second.runs) == (
        {"framing": ["b"], "model": ["m1", "m2", "m3"]},
        2,
        6,
    )
    assert [v.values["model"] for v in second.case.variants] == ["m1", "m2", "m3"]


def test_without_epochs_a_case_keeps_its_own(tmp_path: Path) -> None:
    write(tmp_path, model_case())

    suite = load_suite(
        write_suite(tmp_path, {"schema_version": 1, "id": "s", "cases": [{"path": "../case"}]})
    )

    (entry,) = suite.entries
    assert (entry.overrides, entry.epochs, entry.runs) == ({}, 0, 6)


def test_unknown_keys_and_other_versions_are_rejected(tmp_path: Path) -> None:
    write(tmp_path, model_case())

    unknown = suite_error(
        tmp_path,
        {"schema_version": 1, "id": "s", "cases": [{"path": "../case", "variant": {}}]},
    )
    version = suite_error(tmp_path, {"schema_version": 2, "id": "s", "cases": []})

    assert "`cases[0].variant`: unknown key" in unknown
    assert "`schema_version: 2`" in version and "reads 1" in version


def test_a_case_path_is_relative_to_the_suite_file(tmp_path: Path) -> None:
    write(tmp_path, model_case())

    absolute = suite_error(
        tmp_path,
        {"schema_version": 1, "id": "s", "cases": [{"path": str(tmp_path / "case")}]},
    )
    missing = suite_error(tmp_path, {"schema_version": 1, "id": "s", "cases": [{"path": "case"}]})

    assert "is absolute" in absolute
    assert "`cases[0]` (case)" in missing and "does not exist" in missing


def test_the_model_list_is_set_once(tmp_path: Path) -> None:
    write(tmp_path, model_case())

    message = suite_error(
        tmp_path,
        {
            "schema_version": 1,
            "id": "s",
            "models": ["m1"],
            "cases": [{"path": "../case", "variants": {"model": ["m2"]}}],
        },
    )

    assert "`cases[0].variants.model` repeats what the suite's `models` sets" in message


def test_models_need_a_model_axis_in_every_case(tmp_path: Path) -> None:
    write(tmp_path)

    message = suite_error(
        tmp_path,
        {"schema_version": 1, "id": "s", "models": ["m1"], "cases": [{"path": "../case"}]},
    )

    assert "`cases[0]` (../case)" in message
    assert "`${variant.model}`" in message


def test_a_case_that_does_not_load_names_its_entry(tmp_path: Path) -> None:
    write(tmp_path, model_case())

    message = suite_error(
        tmp_path,
        {
            "schema_version": 1,
            "id": "s",
            "cases": [{"path": "../case", "variants": {"framing": ["c"], "colour": ["x"]}}],
        },
    )

    assert "`cases[0]` (../case)" in message
    assert "`colour` is not an axis" in message
