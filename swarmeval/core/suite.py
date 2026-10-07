"""Suites: a set of cases by a model matrix, `suites/<name>.yaml` (docs/case-format.md#suites).

`load_suite` validates the file and loads every case with the overrides and models the suite
gives it, so a suite that loads submits no case the control plane would refuse.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    ValidationError,
)

from swarmeval.core.loader import (
    CaseError,
    LoadedCase,
    choose_models,
    describe_errors,
    load_case,
)
from swarmeval.core.models import DEFAULT_SLOT, AxisValue, Name

SUITE_SCHEMA_VERSION = 2
"""Version 2 fills the cases' model slots; version 1 filled a `model` variant axis, which cases
no longer have (spec/2026-10-06-run-time-models)."""


class SuiteError(Exception):
    """A suite cannot be loaded. The message names the file, the entry, and the fix."""


def _relative(path: str) -> str:
    if path.startswith("/"):
        raise ValueError(f"`{path}` is absolute. Give a path relative to the suite file.")
    return path


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Axes = dict[Name, Annotated[list[AxisValue], Field(min_length=1)]]
ModelList = Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]
Models = ModelList | dict[Name, ModelList]
"""A list fills the `default` slot; a mapping fills slots by name."""


def _by_slot(models: Models | None) -> dict[str, list[str]]:
    if models is None:
        return {}
    return {DEFAULT_SLOT: models} if isinstance(models, list) else dict(models)


class SuiteCase(_Strict):
    path: Annotated[str, Field(min_length=1), AfterValidator(_relative)]
    """The case directory, relative to the suite file."""
    variants: Axes = Field(default_factory=dict[str, list[AxisValue]])
    """Axis → values, replacing the case's values for that axis."""
    models: Models | None = None
    """Models for this case's slots, replacing the suite's for each slot it names."""
    epochs: PositiveInt | None = None


class SuiteFile(_Strict):
    schema_version: Literal[2]
    id: Name
    description: str | None = None
    models: Models | None = None
    """Models for every case's slots: the model matrix."""
    epochs: PositiveInt | None = None
    """Runs per variant for every case without its own `epochs`. Default: each case's."""
    cases: Annotated[list[SuiteCase], Field(min_length=1)]


@dataclass(frozen=True)
class SuiteEntry:
    path: str
    """The entry's `path`, as the suite file writes it."""
    dir: Path
    overrides: Mapping[str, Sequence[AxisValue]]
    models: Mapping[str, Sequence[str]]
    """Model slot → the models chosen for it, for every slot of the case."""
    epochs: int
    """Runs per variant; 0 means the case's own `epochs`, as `SubmitRuns` takes it."""
    case: LoadedCase

    @property
    def runs(self) -> int:
        return len(self.case.variants) * (self.epochs or self.case.epochs)


@dataclass(frozen=True)
class LoadedSuite:
    source: str
    """Where the suite came from, for messages: its file, or what a caller named it."""
    id: str
    entries: tuple[SuiteEntry, ...]


def load_suite(path: Path) -> LoadedSuite:
    path = path.resolve()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise SuiteError(f"suite file {path} does not exist.") from err
    return load_suite_text(
        text, source=str(path), case_dir=lambda entry: (path.parent / entry).resolve()
    )


def load_suite_text(text: str, *, source: str, case_dir: Callable[[str], Path]) -> LoadedSuite:
    """Loads a suite from its text. `case_dir` maps an entry's `path`, as written, to the case
    directory: next to the suite file on disk, or where the control plane unpacked the bundle
    sent for it. It raises `SuiteError` for a path it has no directory for."""
    try:
        raw: object = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise SuiteError(f"{source} is not valid YAML: {err}") from err
    if isinstance(raw, dict):
        mapping: dict[object, object] = raw  # pyright: ignore[reportUnknownVariableType]
        version = mapping.get("schema_version")
        if version == 1:
            raise SuiteError(
                f"{source} has `schema_version: 1`, whose `models` filled each case's `model` "
                "variant axis. Cases now take models by slot: set `schema_version: 2`; `models` "
                "as a list fills each case's `default` slot, and as a mapping fills slots by "
                "name. See docs/case-format.md#suites."
            )
        if version is not None and version != SUITE_SCHEMA_VERSION:
            raise SuiteError(
                f"{source} has `schema_version: {version}`. This SwarmEval reads "
                f"{SUITE_SCHEMA_VERSION}. Upgrade SwarmEval, or write the suite for a supported "
                "version."
            )
    try:
        suite = SuiteFile.model_validate(raw)
    except ValidationError as err:
        raise SuiteError(describe_errors(source, err)) from err
    suite_models = _by_slot(suite.models)
    entries: list[SuiteEntry] = []
    for i, entry in enumerate(suite.cases):
        where = f"{source} `cases[{i}]` ({entry.path})"
        directory = case_dir(entry.path)
        overrides: dict[str, list[AxisValue]] = dict(entry.variants)
        own = _by_slot(entry.models)
        try:
            loaded = load_case(directory, overrides)
            unknown = [slot for slot in own if slot not in loaded.slots]
            if unknown:
                raise SuiteError(
                    f"{where}: `models` names slots {', '.join(unknown)}, which case "
                    f"`{loaded.label}` does not have. Its slots: {', '.join(loaded.slots)}."
                )
            models = {**suite_models, **own}
            missing = [slot for slot in loaded.slots if slot not in models]
            if missing:
                raise SuiteError(
                    f"{where}: case `{loaded.label}` has model slots {', '.join(loaded.slots)}, "
                    f"and the suite chooses no models for {', '.join(missing)}. Add them under "
                    "the suite's or this entry's `models:`, as `<slot>: [<model>, ...]`."
                )
            chosen = {slot: models[slot] for slot in loaded.slots}
            loaded = choose_models(loaded, chosen)
        except CaseError as err:
            raise SuiteError(f"{where}: {err}") from err
        entries.append(
            SuiteEntry(
                path=entry.path,
                dir=directory,
                overrides=overrides,
                models=chosen,
                epochs=entry.epochs or suite.epochs or 0,
                case=loaded,
            )
        )
    unused = [s for s in suite_models if not any(s in e.case.slots for e in entries)]
    if unused:
        raise SuiteError(
            f"{source}: `models` names slots {', '.join(unused)}, which none of the suite's "
            "cases has. Check the spelling against the cases' `model_slot`s."
        )
    return LoadedSuite(source=source, id=suite.id, entries=tuple(entries))
