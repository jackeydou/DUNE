"""Suites: a set of cases by a model matrix, `suites/<name>.yaml` (docs/case-format.md#suites).

`load_suite` validates the file and loads every case with the overrides the suite gives it, so a
suite that loads submits no case the control plane would refuse.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    ValidationError,
    model_validator,
)

from swarmeval.core.loader import CaseError, LoadedCase, describe_errors, load_case
from swarmeval.core.models import AxisValue, Name

MODEL_AXIS = "model"
"""The variant axis a suite's `models` fills. A case takes it as `model: ${variant.model}`."""


class SuiteError(Exception):
    """A suite cannot be loaded. The message names the file, the entry, and the fix."""


def _relative(path: str) -> str:
    if path.startswith("/"):
        raise ValueError(f"`{path}` is absolute. Give a path relative to the suite file.")
    return path


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Axes = dict[Name, Annotated[list[AxisValue], Field(min_length=1)]]


class SuiteCase(_Strict):
    path: Annotated[str, Field(min_length=1), AfterValidator(_relative)]
    """The case directory, relative to the suite file."""
    variants: Axes = Field(default_factory=dict[str, list[AxisValue]])
    """Axis → values, replacing the case's values for that axis."""
    epochs: PositiveInt | None = None


class SuiteFile(_Strict):
    schema_version: Literal[1]
    id: Name
    description: str | None = None
    models: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)] | None = None
    """Values of every case's `model` axis: the model matrix."""
    epochs: PositiveInt | None = None
    """Runs per variant for every case without its own `epochs`. Default: each case's."""
    cases: Annotated[list[SuiteCase], Field(min_length=1)]

    @model_validator(mode="after")
    def _one_model_list(self) -> Self:
        if self.models is None:
            return self
        for i, entry in enumerate(self.cases):
            if MODEL_AXIS in entry.variants:
                raise ValueError(
                    f"`cases[{i}].variants.{MODEL_AXIS}` repeats what the suite's `models` sets. "
                    "Remove one of them."
                )
        return self


@dataclass(frozen=True)
class SuiteEntry:
    dir: Path
    overrides: Mapping[str, Sequence[AxisValue]]
    epochs: int
    """Runs per variant; 0 means the case's own `epochs`, as `SubmitRuns` takes it."""
    case: LoadedCase

    @property
    def runs(self) -> int:
        return len(self.case.variants) * (self.epochs or self.case.epochs)


@dataclass(frozen=True)
class LoadedSuite:
    path: Path
    id: str
    entries: tuple[SuiteEntry, ...]


def load_suite(path: Path) -> LoadedSuite:
    path = path.resolve()
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise SuiteError(f"suite file {path} does not exist.") from err
    except yaml.YAMLError as err:
        raise SuiteError(f"{path} is not valid YAML: {err}") from err
    if isinstance(raw, dict):
        mapping: dict[object, object] = raw  # pyright: ignore[reportUnknownVariableType]
        version = mapping.get("schema_version", 1)
        if version != 1:
            raise SuiteError(
                f"{path} has `schema_version: {version}`. This SwarmEval reads 1. Upgrade "
                "SwarmEval, or write the suite for a supported version."
            )
    try:
        suite = SuiteFile.model_validate(raw)
    except ValidationError as err:
        raise SuiteError(describe_errors(str(path), err)) from err
    entries: list[SuiteEntry] = []
    for i, entry in enumerate(suite.cases):
        case_dir = (path.parent / entry.path).resolve()
        overrides: dict[str, list[AxisValue]] = dict(entry.variants)
        if suite.models is not None:
            overrides[MODEL_AXIS] = list(suite.models)
        try:
            loaded = load_case(case_dir, overrides)
        except CaseError as err:
            hint = (
                f" The suite's `models` fill each case's `{MODEL_AXIS}` axis; declare it under "
                f"`variants:` and use `${{variant.{MODEL_AXIS}}}` as the agents' model."
                if suite.models is not None and f"`{MODEL_AXIS}` is not an axis" in str(err)
                else ""
            )
            raise SuiteError(f"{path} `cases[{i}]` ({entry.path}): {err}{hint}") from err
        entries.append(
            SuiteEntry(
                dir=case_dir,
                overrides=overrides,
                epochs=entry.epochs or suite.epochs or 0,
                case=loaded,
            )
        )
    return LoadedSuite(path=path, id=suite.id, entries=tuple(entries))
