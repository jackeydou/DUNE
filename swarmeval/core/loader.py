"""Loading a case directory: parse, expand variants, validate, resolve sandboxes and prompts.

This is the boundary for case input. Everything past it trusts the returned models. The
control plane and the worker call the same `load_case`, so a case that loads once loads the
same way everywhere.
"""

import itertools
import logging
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import TypeAdapter, ValidationError

from swarmeval.core.interventions import expand
from swarmeval.core.models import (
    CASE_SCHEMA_VERSIONS,
    ENV_SCHEMA_VERSIONS,
    AxisValue,
    CaseFile,
    CommandScorer,
    CrossSandboxScorer,
    EnvFile,
    EventValueScorer,
    Name,
    RuleScorer,
)
from swarmeval.runtime.extensions import CASE_CODE_PREFIX, ExtensionUse

log = logging.getLogger(__name__)

CASE_FILE = "case.yaml"

_REF = re.compile(r"\$\{variant\.([^}]*)\}")
_NOT_SUBSTITUTED = ("schema_version", "id", "workspace", "variants", "epochs")
"""Case keys that identify the case or size its run matrix, so they cannot vary."""

_AXES: TypeAdapter[dict[str, tuple[AxisValue, ...]]] = TypeAdapter(
    dict[Name, tuple[AxisValue, ...]]
)


class CaseError(Exception):
    """A case directory cannot be loaded. The message names the file, the field, and the fix."""


SEED_LIMIT = 1 << 20
"""sandboxd's limit on the files written into one sandbox at creation, canaries included."""


@dataclass(frozen=True)
class FileSeed:
    path: str
    content: bytes
    mode: int


@dataclass(frozen=True)
class AgentPrompts:
    system: str
    task: str


@dataclass(frozen=True)
class SandboxPlan:
    id: str
    """Shared instances keep their declared name; a private one is named after its agent."""
    profile: str
    shared: bool
    agents: tuple[str, ...]


@dataclass(frozen=True)
class Variant:
    index: int
    values: Mapping[str, AxisValue]
    case: CaseFile
    env: EnvFile
    prompts: Mapping[str, AgentPrompts]
    sandboxes: Mapping[str, SandboxPlan]
    """In order of first reference by an agent."""
    scripts: Mapping[str, str]
    """Command scorer id → its script's text."""
    files: Mapping[str, tuple[FileSeed, ...]]
    """Sandbox instance → the case files its profile copies in."""
    extensions: tuple[ExtensionUse, ...]
    """What the run loads: `extensions:`, then the channels' `interventions:` expanded."""
    code: Mapping[str, str]
    """`case:` extension reference → the source text of the file it names. Read at load, so a
    run imports exactly what was submitted; whether it may is the deployment's
    `allow_case_code`."""
    warnings: tuple[str, ...]

    def sandbox_of(self, agent_id: str) -> SandboxPlan:
        return next(s for s in self.sandboxes.values() if agent_id in s.agents)


@dataclass(frozen=True)
class LoadedCase:
    dir: Path
    id: str
    workspace: str
    epochs: int
    variants: tuple[Variant, ...]
    """Cartesian product of the variant axes, first axis varying slowest."""
    warnings: tuple[str, ...]
    """Things that load but do nothing, once each across variants. Also logged."""


def load_case(
    case_dir: Path, overrides: Mapping[str, Sequence[AxisValue]] | None = None
) -> LoadedCase:
    """Loads and validates every variant. `overrides` replaces the values of declared axes."""
    case_dir = case_dir.resolve()
    case_path = case_dir / CASE_FILE
    raw_case = _read_yaml(case_path)
    version = _check_version(case_path, raw_case, CASE_SCHEMA_VERSIONS)
    axes = _axes(case_path, raw_case.get("variants", {}), overrides or {})
    if version < 2:
        _refuse_v2_axes(case_path, axes)
    for key in _NOT_SUBSTITUTED:
        if key in raw_case and _references(raw_case[key]):
            raise CaseError(
                f"{case_path}: `{key}` refers to a variant. `{key}` is fixed for the whole "
                "case; vary another field instead."
            )

    files = _Files(case_dir)
    variants: list[Variant] = []
    names = list(axes)
    for index, combo in enumerate(itertools.product(*axes.values())):
        values = dict(zip(names, combo, strict=True))
        variants.append(_variant(files, case_path, raw_case, index, values, version))
    first = variants[0].case
    warnings = tuple(dict.fromkeys(w for v in variants for w in v.warnings))
    for warning in warnings:
        log.warning("%s: %s", case_path, warning)
    return LoadedCase(
        dir=case_dir,
        id=first.id,
        workspace=first.workspace,
        epochs=first.epochs,
        variants=tuple(variants),
        warnings=warnings,
    )


def _variant(
    files: "_Files",
    case_path: Path,
    raw_case: dict[str, object],
    index: int,
    values: dict[str, AxisValue],
    version: int,
) -> Variant:
    where = f"{case_path}" + (f" (variant {values})" if values else "")
    substituted = {
        key: value if key in _NOT_SUBSTITUTED else _substitute(value, values, where, (key,))
        for key, value in raw_case.items()
    }
    case = _validate(CaseFile, substituted, where)
    if version < 2:
        _refuse_v2_fields(case, where)
    if version < 3:
        _refuse_v3_fields(case, where)
    try:
        expanded = expand(case)
    except ValueError as err:
        raise CaseError(f"{where}: {err}") from err

    env_path = files.resolve(case.environment, "environment")
    raw_env = files.yaml(env_path)
    _check_version(env_path, raw_env, ENV_SCHEMA_VERSIONS)
    env_where = f"{env_path}" + (f" (variant {values})" if values else "")
    env = _validate(EnvFile, _substitute(raw_env, values, env_where, ()), env_where)

    sandboxes = _sandboxes(case, env, env_path)
    _check_canaries(env, sandboxes, env_where)
    seeds = _seeds(env, sandboxes, files, env_where)
    scripts: dict[str, str] = {}
    for scorer in case.scorers:
        if isinstance(scorer, CommandScorer):
            if scorer.sandbox not in sandboxes:
                raise CaseError(
                    f"{where}: scorer `{scorer.id}` runs in sandbox `{scorer.sandbox}`, which no "
                    f"agent uses. Sandboxes: {', '.join(sandboxes)}."
                )
            scripts[scorer.id] = files.text(scorer.script, f"scorers[{scorer.id}].script")
        if isinstance(scorer, CrossSandboxScorer) and len(sandboxes) < 2:
            raise CaseError(
                f"{where}: scorer `{scorer.id}` looks for information crossing between sandboxes, "
                f"but every agent uses sandbox `{next(iter(sandboxes))}`. Give agents their own "
                "sandboxes, or remove the scorer."
            )
    code = {
        use.use: _case_code(files, use.use, f"extensions[{i}].use")
        for i, use in enumerate(case.extensions)
        if use.use.startswith(CASE_CODE_PREFIX)
    }
    prompts: dict[str, AgentPrompts] = {}
    for agent in case.swarm.agents:
        field = f"swarm.agents[{agent.id}]"
        if agent.task is not None:
            task_path, task_field = agent.task, f"{field}.task"
        else:
            assert case.task is not None, "CaseFile checks every agent has a task"
            task_path, task_field = case.task.input, "task.input"
        prompts[agent.id] = AgentPrompts(
            system=files.text(agent.prompt, f"{field}.prompt"),
            task=files.text(task_path, task_field),
        )
    return Variant(
        index=index,
        values=values,
        case=case,
        env=env,
        prompts=prompts,
        sandboxes=sandboxes,
        scripts=scripts,
        files=seeds,
        extensions=expanded.extensions,
        code=code,
        warnings=expanded.warnings,
    )


def _case_code(files: "_Files", use: str, field: str) -> str:
    relative = use.removeprefix(CASE_CODE_PREFIX)
    if not relative.endswith(".py"):
        raise CaseError(
            f"`{field}` is `{use}`, which does not name a Python file. A `case:` reference is "
            "a `.py` file in the case directory, like `case:extensions/market.py`."
        )
    return files.text(relative, field)


def _check_canaries(env: EnvFile, sandboxes: Mapping[str, SandboxPlan], where: str) -> None:
    for canary in env.canaries:
        plan = sandboxes.get(canary.sandbox)
        if plan is None:
            raise CaseError(
                f"{where}: canary `{canary.id}` goes in sandbox `{canary.sandbox}`, which no "
                f"agent uses. Sandboxes: {', '.join(sandboxes)}."
            )
        mounts = [m.path for m in env.sandbox_profiles[plan.profile].fs]
        if not any(canary.path.startswith(m + "/") for m in mounts):
            raise CaseError(
                f"{where}: canary `{canary.id}` path `{canary.path}` is not inside a key path of "
                f"sandbox `{canary.sandbox}` (profile `{plan.profile}`: "
                f"{', '.join(mounts) or 'no key paths'}). Canaries are written into key paths "
                "when the sandbox is created."
            )


def _sandboxes(case: CaseFile, env: EnvFile, env_path: Path) -> dict[str, SandboxPlan]:
    agents_of: dict[str, list[str]] = {}
    profile_of: dict[str, str] = {}
    for agent in case.swarm.agents:
        if agent.sandbox is not None:
            instance = env.sandboxes.get(agent.sandbox)
            if instance is None:
                raise CaseError(
                    f"agent `{agent.id}` uses sandbox `{agent.sandbox}`, which {env_path} does "
                    f"not declare. Declared: {', '.join(env.sandboxes) or 'none'}. Add it under "
                    "`sandboxes:`, or use `sandbox_profile:` for a private sandbox."
                )
            name, profile = agent.sandbox, instance.profile
        else:
            if agent.id in env.sandboxes:
                raise CaseError(
                    f"agent `{agent.id}` gets a private sandbox named `{agent.id}`, but "
                    f"{env_path} also declares a shared sandbox `{agent.id}`. Rename the shared "
                    "sandbox."
                )
            name, profile = agent.id, agent.sandbox_profile or "default"
            if profile not in env.sandbox_profiles:
                fix = (
                    "Add a `default` profile, or set `sandbox_profile:` or `sandbox:` on it."
                    if agent.sandbox_profile is None
                    else "Declare it under `sandbox_profiles:`."
                )
                raise CaseError(
                    f"agent `{agent.id}` needs sandbox profile `{profile}`, which {env_path} "
                    f"does not declare. Profiles: {', '.join(env.sandbox_profiles) or 'none'}. "
                    f"{fix}"
                )
        agents_of.setdefault(name, []).append(agent.id)
        profile_of[name] = profile

    unused = [name for name in env.sandboxes if name not in agents_of]
    if unused:
        raise CaseError(
            f"{env_path} declares shared sandboxes {', '.join(f'`{u}`' for u in unused)} that no "
            "agent uses. Point an agent at each with `sandbox:`, or remove them."
        )
    return {
        name: SandboxPlan(
            id=name,
            profile=profile_of[name],
            shared=name in env.sandboxes,
            agents=tuple(agents),
        )
        for name, agents in agents_of.items()
    }


def _seeds(
    env: EnvFile, sandboxes: Mapping[str, SandboxPlan], files: "_Files", where: str
) -> dict[str, tuple[FileSeed, ...]]:
    by_profile: dict[str, tuple[FileSeed, ...]] = {}
    for name, profile in env.sandbox_profiles.items():
        mounts = [m.path for m in profile.fs]
        seeds: list[FileSeed] = []
        for i, copy in enumerate(profile.files):
            field = f"sandbox_profiles.{name}.files[{i}]"
            for rel, path in files.tree(copy.source, f"{field}.from"):
                target = posixpath.join(copy.to, rel) if rel else copy.to
                if not any(target.startswith(m + "/") for m in mounts):
                    raise CaseError(
                        f"{where}: `{field}` copies to `{target}`, which is not inside a key path "
                        f"of profile `{name}` ({', '.join(mounts) or 'none'}). Files are written "
                        "into key paths when the sandbox is created."
                    )
                seeds.append(FileSeed(target, path.read_bytes(), path.stat().st_mode & 0o777))
        by_profile[name] = tuple(seeds)
    result: dict[str, tuple[FileSeed, ...]] = {}
    for plan in sandboxes.values():
        copied = by_profile[plan.profile]
        paths = {s.path for s in copied}
        canaries = [c for c in env.canaries if c.sandbox == plan.id]
        for canary in canaries:
            if canary.path in paths:
                raise CaseError(
                    f"{where}: canary `{canary.id}` and a copied case file both write "
                    f"`{canary.path}` in sandbox `{plan.id}`. Move one of them."
                )
        size = sum(len(s.content) for s in copied) + sum(len(c.template) + 64 for c in canaries)
        if size > SEED_LIMIT:
            raise CaseError(
                f"{where}: sandbox `{plan.id}` would get {size} bytes of case files and canaries, "
                f"over the {SEED_LIMIT} byte limit. Put large data in the sandbox image instead."
            )
        result[plan.id] = copied
    return result


class _Files:
    """Reads files inside the case directory, once each. A path that leaves it is an error:
    case bundles are uploaded, and a bundle must not read the host."""

    def __init__(self, case_dir: Path) -> None:
        self._dir = case_dir
        self._yaml: dict[Path, dict[str, object]] = {}
        self._text: dict[Path, str] = {}

    def resolve(self, relative: str, field: str) -> Path:
        path = (self._dir / relative).resolve()
        if not path.is_relative_to(self._dir):
            raise CaseError(
                f"case {self._dir}: `{field}` points to `{relative}`, which is outside the case "
                "directory. Keep every file the case uses inside it."
            )
        if not path.is_file():
            raise CaseError(
                f"case {self._dir}: `{field}` points to `{relative}`, which does not exist or "
                "is not a file."
            )
        return path

    def tree(self, relative: str, field: str) -> list[tuple[str, Path]]:
        """A file as `[("", path)]`, or every file under a directory with its path relative to
        it, in order. Nothing may resolve outside the case directory."""
        root = (self._dir / relative).resolve()
        if not root.is_relative_to(self._dir) or not root.exists():
            raise CaseError(
                f"case {self._dir}: `{field}` points to `{relative}`, which is outside the case "
                "directory or does not exist."
            )
        if root.is_file():
            return [("", root)]
        found: list[tuple[str, Path]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(self._dir):
                raise CaseError(
                    f"case {self._dir}: `{field}` contains `{path.relative_to(self._dir)}`, a "
                    "link to outside the case directory."
                )
            found.append((path.relative_to(root).as_posix(), resolved))
        return found

    def yaml(self, path: Path) -> dict[str, object]:
        if path not in self._yaml:
            self._yaml[path] = _read_yaml(path)
        return self._yaml[path]

    def text(self, relative: str, field: str) -> str:
        path = self.resolve(relative, field)
        if path not in self._text:
            try:
                self._text[path] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as err:
                raise CaseError(f"{path} (`{field}`) is not UTF-8 text: {err}") from err
        return self._text[path]


def _read_yaml(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise CaseError(f"{path} does not exist. A case directory needs `{CASE_FILE}`.") from err
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise CaseError(f"{path} is not valid YAML: {err}") from err
    if not isinstance(data, dict):
        raise CaseError(f"{path} must be a mapping at the top level, got {type(data).__name__}.")
    mapping: dict[object, object] = data  # pyright: ignore[reportUnknownVariableType]
    return {str(k): v for k, v in mapping.items()}


def _check_version(path: Path, raw: dict[str, object], supported: frozenset[int]) -> int:
    version = raw.get("schema_version")
    if version is None:
        raise CaseError(f"{path} has no `schema_version`. Add `schema_version: {max(supported)}`.")
    if not isinstance(version, int) or isinstance(version, bool) or version not in supported:
        raise CaseError(
            f"{path} has `schema_version: {version}`. This SwarmEval reads "
            f"{', '.join(map(str, sorted(supported)))}. Upgrade SwarmEval, or write the "
            "file for a supported version."
        )
    return version


def _refuse_v2_axes(path: Path, axes: Mapping[str, tuple[AxisValue, ...]]) -> None:
    for name, values in axes.items():
        if any(isinstance(v, tuple) for v in values):
            raise CaseError(
                f"{path}: variant axis `{name}` has a list value, which needs "
                "`schema_version: 2`. Raise the case's `schema_version` to 2."
            )


def _refuse_v2_fields(case: CaseFile, where: str) -> None:
    for channel in case.swarm.channels:
        if channel.interventions:
            raise CaseError(
                f"{where}: channel `{channel.id}` lists `interventions`, which needs "
                "`schema_version: 2`. Raise the case's `schema_version` to 2."
            )
    for scorer in case.scorers:
        if isinstance(scorer, CrossSandboxScorer):
            raise CaseError(
                f"{where}: scorer `{scorer.id}` has type `cross_sandbox`, which needs "
                "`schema_version: 2`. Raise the case's `schema_version` to 2."
            )


def _refuse_v3_fields(case: CaseFile, where: str) -> None:
    if case.swarm.turn_policy != "round_robin":
        raise CaseError(
            f"{where}: `swarm.turn_policy` is `{case.swarm.turn_policy}`, which needs "
            "`schema_version: 3`. Raise the case's `schema_version` to 3."
        )
    if case.swarm.limits.wall_clock is not None:
        raise CaseError(
            f"{where}: `swarm.limits.wall_clock` needs `schema_version: 3`. Raise the case's "
            "`schema_version` to 3."
        )
    for i, use in enumerate(case.extensions):
        if use.use.startswith(CASE_CODE_PREFIX):
            raise CaseError(
                f"{where}: `extensions[{i}]` loads `{use.use}` from the case directory, which "
                "needs `schema_version: 3`. Raise the case's `schema_version` to 3."
            )
    for scorer in case.scorers:
        if isinstance(scorer, EventValueScorer | RuleScorer):
            raise CaseError(
                f"{where}: scorer `{scorer.id}` has type `{scorer.type}`, which needs "
                "`schema_version: 3`. Raise the case's `schema_version` to 3."
            )


def _axes(
    path: Path, raw: object, overrides: Mapping[str, Sequence[AxisValue]]
) -> dict[str, tuple[AxisValue, ...]]:
    try:
        axes = _AXES.validate_python(raw)
    except ValidationError as err:
        raise CaseError(describe_errors(f"{path} `variants`", err)) from err
    for name, values in overrides.items():
        if name not in axes:
            raise CaseError(
                f"variant override `{name}` is not an axis of {path}. "
                f"Axes: {', '.join(axes) or 'none'}."
            )
        axes[name] = tuple(values)
    for name, values in axes.items():
        if not values:
            raise CaseError(f"variant axis `{name}` of {path} has no values. Give at least one.")
        if len(set(map(repr, values))) != len(values):
            raise CaseError(
                f"variant axis `{name}` of {path} repeats a value: {list(values)}. "
                "Each value makes a variant; list it once."
            )
    return axes


def _references(node: object) -> bool:
    match node:
        case str():
            return _REF.search(node) is not None
        case list():
            items: list[object] = node  # pyright: ignore[reportUnknownVariableType]
            return any(_references(v) for v in items)
        case dict():
            mapping: dict[object, object] = node  # pyright: ignore[reportUnknownVariableType]
            return any(_references(v) for v in mapping.values())
        case _:
            return False


def _substitute(
    node: object, values: Mapping[str, AxisValue], where: str, path: tuple[str | int, ...]
) -> object:
    """A string that is exactly one reference takes the value with its type; a reference
    inside a longer string is replaced by the value's text."""
    match node:
        case str():
            whole = _REF.fullmatch(node)
            if whole is not None:
                value = _value(whole.group(1), values, where, path)
                return list(value) if isinstance(value, tuple) else value
            return _REF.sub(lambda m: _text(m.group(1), values, where, path), node)
        case list():
            items: list[object] = node  # pyright: ignore[reportUnknownVariableType]
            return [_substitute(v, values, where, (*path, i)) for i, v in enumerate(items)]
        case dict():
            mapping: dict[object, object] = node  # pyright: ignore[reportUnknownVariableType]
            return {k: _substitute(v, values, where, (*path, str(k))) for k, v in mapping.items()}
        case _:
            return node


def _value(
    name: str, values: Mapping[str, AxisValue], where: str, path: tuple[str | int, ...]
) -> AxisValue:
    if name not in values:
        raise CaseError(
            f"{where}: `{_dotted(path)}` refers to `${{variant.{name}}}`, but the case has no "
            f"variant axis `{name}`. Axes: {', '.join(values) or 'none'}. Declare it under "
            "`variants:`."
        )
    return values[name]


def _text(
    name: str, values: Mapping[str, AxisValue], where: str, path: tuple[str | int, ...]
) -> str:
    value = _value(name, values, where, path)
    if isinstance(value, tuple):
        raise CaseError(
            f"{where}: `{_dotted(path)}` puts `${{variant.{name}}}`, a list, inside a longer "
            "string. A list-valued axis can only be a field's whole value."
        )
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _validate[M: CaseFile | EnvFile](model: type[M], raw: object, where: str) -> M:
    try:
        return model.model_validate(raw)
    except ValidationError as err:
        raise CaseError(describe_errors(where, err)) from err


def describe_errors(where: str, err: ValidationError) -> str:
    """One line per problem, naming its field. Shared by case, env, and suite files, which
    docs/case-format.md all documents."""
    lines = [f"{where}: {err.error_count()} problem(s)"]
    for detail in err.errors():
        location = _dotted(detail["loc"]) or "(top level)"
        message = detail["msg"].removeprefix("Value error, ")
        if detail["type"] == "extra_forbidden":
            message = "unknown key. Remove it, or check the spelling against docs/case-format.md."
        elif detail["type"] == "missing":
            message = "required, but missing."
        lines.append(f"  `{location}`: {message}")
    return "\n".join(lines)


def _dotted(path: Sequence[str | int]) -> str:
    return "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in path).lstrip(".")
