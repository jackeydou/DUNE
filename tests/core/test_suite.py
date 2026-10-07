from pathlib import Path
from typing import Any

import pytest
import yaml

from swarmeval.core import SuiteError, load_suite
from tests.core.test_loader import base_case, write

REPO = Path(__file__).resolve().parents[2]


def framed_case() -> dict[str, Any]:
    case = base_case()
    case["variants"] = {"framing": ["a", "b"]}
    case["epochs"] = 3
    return case


def two_slot_case() -> dict[str, Any]:
    case = base_case()
    case["id"] = "duel"
    case["swarm"]["agents"][0]["model_slot"] = "attacker"
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
    assert len(entry.models["default"]) >= 3
    assert entry.epochs == 10


def test_a_model_list_fills_the_default_slot_and_epochs_fall_back_in_order(
    tmp_path: Path,
) -> None:
    write(tmp_path, framed_case())
    path = write_suite(
        tmp_path,
        {
            "schema_version": 2,
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
    assert (first.overrides, first.models, first.epochs, first.runs) == (
        {},
        {"default": ["m1", "m2", "m3"]},
        10,
        60,
    )
    assert (second.overrides, second.epochs, second.runs) == ({"framing": ["b"]}, 2, 6)
    assert [v.models["default"] for v in second.case.variants] == ["m1", "m2", "m3"]


def test_a_model_mapping_fills_slots_by_name_and_an_entry_replaces_a_slot(
    tmp_path: Path,
) -> None:
    write(tmp_path, two_slot_case())
    path = write_suite(
        tmp_path,
        {
            "schema_version": 2,
            "id": "duels",
            "models": {"attacker": ["a1", "a2"], "default": ["d1"]},
            "cases": [{"path": "../case"}, {"path": "../case", "models": {"attacker": ["a3"]}}],
        },
    )

    first, second = load_suite(path).entries

    assert first.models == {"attacker": ["a1", "a2"], "default": ["d1"]}
    assert first.runs == 2
    assert second.models == {"attacker": ["a3"], "default": ["d1"]}


def test_without_epochs_a_case_keeps_its_own(tmp_path: Path) -> None:
    write(tmp_path, framed_case())

    suite = load_suite(
        write_suite(
            tmp_path,
            {"schema_version": 2, "id": "s", "models": ["m1"], "cases": [{"path": "../case"}]},
        )
    )

    (entry,) = suite.entries
    assert (entry.overrides, entry.epochs, entry.runs) == ({}, 0, 6)


def test_every_slot_of_every_case_needs_models(tmp_path: Path) -> None:
    write(tmp_path, two_slot_case())

    message = suite_error(
        tmp_path,
        {"schema_version": 2, "id": "s", "models": ["m1"], "cases": [{"path": "../case"}]},
    )

    assert "`cases[0]` (../case)" in message
    assert "has model slots attacker, default" in message
    assert "chooses no models for attacker" in message


def test_a_slot_no_case_has_is_a_typo(tmp_path: Path) -> None:
    write(tmp_path, framed_case())

    suite_wide = suite_error(
        tmp_path,
        {
            "schema_version": 2,
            "id": "s",
            "models": {"default": ["m1"], "atacker": ["m2"]},
            "cases": [{"path": "../case"}],
        },
    )
    entry = suite_error(
        tmp_path,
        {
            "schema_version": 2,
            "id": "s",
            "models": ["m1"],
            "cases": [{"path": "../case", "models": {"atacker": ["m2"]}}],
        },
    )

    assert "`models` names slots atacker, which none of the suite's cases has" in suite_wide
    assert "`cases[0]` (../case): `models` names slots atacker" in entry


def test_unknown_keys_and_other_versions_are_rejected(tmp_path: Path) -> None:
    write(tmp_path, framed_case())

    unknown = suite_error(
        tmp_path,
        {"schema_version": 2, "id": "s", "cases": [{"path": "../case", "variant": {}}]},
    )
    version = suite_error(tmp_path, {"schema_version": 3, "id": "s", "cases": []})
    retired = suite_error(tmp_path, {"schema_version": 1, "id": "s", "cases": []})

    assert "`cases[0].variant`: unknown key" in unknown
    assert "`schema_version: 3`" in version and "reads 2" in version
    assert "`schema_version: 1`, whose `models` filled each case's `model`" in retired


def test_a_case_path_is_relative_to_the_suite_file(tmp_path: Path) -> None:
    write(tmp_path, framed_case())

    absolute = suite_error(
        tmp_path,
        {"schema_version": 2, "id": "s", "cases": [{"path": str(tmp_path / "case")}]},
    )
    missing = suite_error(tmp_path, {"schema_version": 2, "id": "s", "cases": [{"path": "case"}]})

    assert "is absolute" in absolute
    assert "`cases[0]` (case)" in missing and "does not exist" in missing


def test_a_case_that_does_not_load_names_its_entry(tmp_path: Path) -> None:
    write(tmp_path, framed_case())

    message = suite_error(
        tmp_path,
        {
            "schema_version": 2,
            "id": "s",
            "models": ["m1"],
            "cases": [{"path": "../case", "variants": {"framing": ["c"], "colour": ["x"]}}],
        },
    )

    assert "`cases[0]` (../case)" in message
    assert "`colour` is not an axis" in message
