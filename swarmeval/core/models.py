"""The `case.yaml` and `env.yaml` formats, schema version 1. The user-facing contract is
docs/case-format.md; a change here changes that page.

These models see a file after variant substitution. Checks that span both files (sandbox
topology) and touch the case directory (prompt files) are in the loader.
"""

import posixpath
import re
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ByteSize,
    ConfigDict,
    Field,
    JsonValue,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)

from swarmeval.detect.defs import DetectorDef
from swarmeval.runtime.extensions import ExtensionUse

CASE_SCHEMA_VERSIONS = frozenset({1, 2, 3})
"""`case.yaml` versions read. Version 2 adds channel `interventions`, list values for variant
axes, and the `cross_sandbox` scorer. Version 3 adds `case:` extension references and the
`event_value` and `rule` scorers, the `event_driven` and `async` turn policies, and
`limits.wall_clock`. Older cases read unchanged and refuse what came after them."""
ENV_SCHEMA_VERSIONS = frozenset({1})

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=63)]
"""Ids of cases, agents, channels, sandboxes, profiles, and variant axes. They end up in
container names, labels, and event fields, so they are kept to one safe alphabet."""

Workspace = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=63)]
UnixUser = Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_-]*$", max_length=32)]
Scalar = str | int | float | bool
AxisValue = Scalar | tuple[Scalar, ...]
"""A variant axis value: a scalar, or a list of scalars, such as the channels an intervention
applies to (`[]` for none)."""


def axis_json(value: AxisValue) -> JsonValue:
    """The value as stored in `task_args` and overrides: a list as a JSON array."""
    return list(value) if isinstance(value, tuple) else value


def _relative(path: str) -> str:
    if path.startswith("/"):
        raise ValueError(f"`{path}` is absolute. Give a path relative to the case directory.")
    return path


def _mount_path(path: str) -> str:
    """The rule sandboxd applies to key paths, checked here so a bad case fails at load."""
    if not path.startswith("/"):
        raise ValueError(f"`{path}` is relative. Mount paths inside a sandbox are absolute.")
    if path == "/":
        raise ValueError(
            "`/` cannot be a mount path. Mount a directory under it, like `/workspace`."
        )
    clean = posixpath.normpath(path)
    if clean != path or path.startswith("//"):
        raise ValueError(
            f"`{path}` is not a clean path. Write it without `.`, `..`, repeated or trailing "
            f"slashes: `{clean.replace('//', '/')}`."
        )
    return path


RelPath = Annotated[str, Field(min_length=1), AfterValidator(_relative)]
SandboxPath = Annotated[str, AfterValidator(_mount_path)]

_COUNT = re.compile(r"^(\d+(?:\.\d+)?)([km]?)$")


def _count(value: object) -> object:
    """`400k` → 400000, `1.5m` → 1500000. Other input passes through to int validation."""
    if not isinstance(value, str):
        return value
    match = _COUNT.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError(f"`{value}` is not a count. Write an integer, or one like `400k`, `2m`.")
    number, suffix = match.groups()
    scaled = float(number) * {"": 1, "k": 1_000, "m": 1_000_000}[suffix]
    if not scaled.is_integer():
        raise ValueError(f"`{value}` is not a whole number.")
    return int(scaled)


Count = Annotated[PositiveInt, BeforeValidator(_count)]

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(s|m|h)$")


def _duration(value: object) -> object:
    """`90s`, `20m`, `1.5h` → seconds. A bare number is seconds."""
    if not isinstance(value, str):
        return value
    match = _DURATION.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError(f"`{value}` is not a duration. Write one like `90s`, `20m`, or `2h`.")
    number, unit = match.groups()
    return float(number) * {"s": 1, "m": 60, "h": 3600}[unit]


Duration = Annotated[PositiveFloat, BeforeValidator(_duration)]
"""Seconds."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Sampling(Strict):
    temperature: Annotated[float, Field(ge=0)] | None = None
    top_p: Annotated[float, Field(gt=0, le=1)] | None = None
    max_output_tokens: PositiveInt | None = None
    seed: int | None = None


class AgentDef(Strict):
    id: Name
    model: Annotated[str, Field(min_length=1)]
    prompt: RelPath
    """System prompt file."""
    task: RelPath | None = None
    """First user message file. Overrides the case's `task.input`."""
    tools: tuple[str, ...] = ()
    sandbox: Name | None = None
    """A shared instance declared under `sandboxes:` in `env.yaml`."""
    sandbox_profile: Name | None = None
    """Profile of this agent's private sandbox. Neither field set means profile `default`."""
    os_user: UnixUser | None = None
    sampling: Sampling = Sampling()

    @model_validator(mode="after")
    def _one_sandbox_field(self) -> Self:
        if self.sandbox is not None and self.sandbox_profile is not None:
            raise ValueError(
                f"agent `{self.id}` sets both `sandbox` and `sandbox_profile`. Use `sandbox` to "
                "join a shared instance (its profile comes from `env.yaml`), or "
                "`sandbox_profile` for a private one."
            )
        return self


INTERVENTIONS = ("log", "drop", "delay", "paraphrase", "inject")
InterventionItem = str | dict[str, dict[str, JsonValue]]
"""A name, or a one-key mapping of a name to its config without the channel."""


class ChannelDef(Strict):
    id: Name
    members: Annotated[tuple[Name, ...], Field(min_length=2)]
    interventions: tuple[InterventionItem, ...] = ()
    """Shorthand for the built-in `swarmeval.bus.*` extensions on this channel; the loader
    expands it (`swarmeval.core.interventions`)."""

    @field_validator("interventions")
    @classmethod
    def _known(cls, items: tuple[InterventionItem, ...]) -> tuple[InterventionItem, ...]:
        for item in items:
            if isinstance(item, dict) and len(item) != 1:
                raise ValueError(
                    f"intervention {item} names {len(item)} interventions. Write one per entry, "
                    "like `- delay: {turns: 2}`."
                )
            name = item if isinstance(item, str) else next(iter(item))
            if name not in INTERVENTIONS:
                raise ValueError(
                    f"unknown intervention `{name}`. Known: {', '.join(INTERVENTIONS)}."
                )
        return items


class Limits(Strict):
    max_turns: PositiveInt | None = None
    """All agents together; under `async`, each agent's own."""
    max_tokens: Count | None = None
    wall_clock: Duration | None = None
    """(v3) Run time, paused time left out."""


class SwarmDef(Strict):
    agents: Annotated[tuple[AgentDef, ...], Field(min_length=1)]
    channels: tuple[ChannelDef, ...] = ()
    turn_policy: Literal["round_robin", "event_driven", "async"] = "round_robin"
    """`event_driven` and `async` need case schema version 3."""
    limits: Limits = Limits()

    @model_validator(mode="after")
    def _references(self) -> Self:
        agent_ids = [a.id for a in self.agents]
        _no_duplicates("agent", agent_ids)
        _no_duplicates("channel", [c.id for c in self.channels])
        for channel in self.channels:
            _no_duplicates(f"member of channel `{channel.id}`", list(channel.members))
            for member in channel.members:
                if member not in agent_ids:
                    raise ValueError(
                        f"channel `{channel.id}` lists unknown member `{member}`. "
                        f"Agents: {', '.join(agent_ids)}. Add it under `swarm.agents` or "
                        "remove it from the channel."
                    )
        return self


class TaskDef(Strict):
    input: RelPath
    """First user message file for every agent without its own `task`."""


class ProtectedWriteScorer(Strict):
    id: Name
    type: Literal["protected_write"]
    meaning: str = "something wrote under a protected path"
    ignore: tuple[str, ...] = ()
    """`fnmatch` patterns over sandbox paths that do not count, such as `*/__pycache__/*`. `*`
    matches across `/`."""


class CanaryScorer(Strict):
    id: Name
    type: Literal["canary"]
    meaning: str = (
        "a canary appeared in model output, tool output, a message, or a file the run wrote"
    )


class CrossSandboxScorer(Strict):
    id: Name
    type: Literal["cross_sandbox"]
    meaning: str = (
        "a sandbox's canary reached an agent outside that sandbox other than in a message on a "
        "declared channel"
    )


class CommandScorer(Strict):
    """Runs `script` from the case directory in a sandbox after the agents stop. The script's
    text is the last argument after `interpreter`, so the sandbox never holds it before then."""

    id: Name
    type: Literal["command"]
    sandbox: Name
    script: RelPath
    interpreter: Annotated[tuple[str, ...], Field(min_length=1)] = ("sh", "-c")
    timeout_s: PositiveFloat = 120.0
    triggered: Literal["nonzero_exit", "zero_exit"] = "nonzero_exit"
    meaning: Annotated[str, Field(min_length=1)]
    """What a score of 1 means, in words."""


class EventValueScorer(Strict):
    """Compares one field of the last `extension` event named `event` with `threshold`. A run
    with no such event scores 0."""

    id: Name
    type: Literal["event_value"]
    event: Annotated[str, Field(min_length=1)]
    """The name an extension emitted the event under (`ctx.emit(name, data)`)."""
    extension: str | None = None
    """Only events this extension instance emitted. Default: any instance."""
    field: Annotated[str, Field(pattern=r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")]
    """Dotted path into the event's data, such as `index` or `totals.mean_index`."""
    op: Literal[">=", ">", "<=", "<"] = ">="
    threshold: Annotated[float, Field(allow_inf_nan=False)]
    """No default: what a meaningful threshold is depends on the field. Finite: every comparison
    with NaN is false, which would score every run 0."""
    meaning: Annotated[str, Field(min_length=1)]


class RuleScorer(Strict):
    """One detector over the run's events after the agents stop: 1 when it hit at all. The
    same detectors the Monitor runs online (`swarmeval.detect`)."""

    id: Name
    type: Literal["rule"]
    detect: DetectorDef
    meaning: Annotated[str, Field(min_length=1)]


ScorerDef = Annotated[
    ProtectedWriteScorer
    | CanaryScorer
    | CrossSandboxScorer
    | CommandScorer
    | EventValueScorer
    | RuleScorer,
    Field(discriminator="type"),
]


class CaseFile(Strict):
    schema_version: int
    id: Name
    workspace: Workspace
    category: str | None = None
    description: str | None = None
    variants: dict[Name, Annotated[tuple[AxisValue, ...], Field(min_length=1)]] = Field(
        default_factory=dict[str, tuple[AxisValue, ...]]
    )
    epochs: PositiveInt = 1
    swarm: SwarmDef
    environment: RelPath = "env.yaml"
    task: TaskDef | None = None
    extensions: tuple[ExtensionUse, ...] = ()
    scorers: tuple[ScorerDef, ...] = ()

    @model_validator(mode="after")
    def _unique_scorers(self) -> Self:
        _no_duplicates("scorer", [s.id for s in self.scorers])
        return self

    @model_validator(mode="after")
    def _every_agent_has_a_task(self) -> Self:
        if self.task is None:
            missing = [a.id for a in self.swarm.agents if a.task is None]
            if missing:
                raise ValueError(
                    f"agents {', '.join(f'`{m}`' for m in missing)} have no task. Set "
                    "`task.input` for the whole case, or `task` on each agent."
                )
        return self


class Mount(Strict):
    path: SandboxPath
    mode: Literal["rw", "ro"] = "rw"
    protected: bool = False
    """Any write here is a high-priority event."""


class ResourceLimits(Strict):
    cpu: PositiveFloat | None = None
    memory: ByteSize | None = None
    """`2g` is 2 * 10**9 bytes; write `2gib` for 2 * 2**30."""
    pids: PositiveInt | None = None
    disk: ByteSize | None = None


class FileCopy(Strict):
    """Case files copied into the sandbox when it is created, as part of its baseline."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    source: RelPath = Field(alias="from")
    """A file or directory in the case directory."""
    to: SandboxPath
    """Where it lands. A directory's contents go under it."""


class SandboxProfile(Strict):
    image: Annotated[str, Field(min_length=1)]
    fs: tuple[Mount, ...] = ()
    limits: ResourceLimits = ResourceLimits()
    files: tuple[FileCopy, ...] = ()

    @model_validator(mode="after")
    def _unique_mounts(self) -> Self:
        _no_duplicates("mount path", [m.path for m in self.fs])
        return self


class SandboxInstance(Strict):
    profile: Name


CANARY_SLOT = "{{canary}}"


def _has_slot(template: str) -> str:
    if CANARY_SLOT not in template:
        raise ValueError(
            f"a canary template must contain `{CANARY_SLOT}`, where the run's token goes."
        )
    return template


class CanaryDef(Strict):
    """A file holding a token generated per run. Any later sighting of the token is a hit."""

    id: Name
    sandbox: Name
    """A shared instance's name, or an agent's id for its private sandbox."""
    path: SandboxPath
    """Inside one of the sandbox's key paths."""
    template: Annotated[str, AfterValidator(_has_slot)]


class EnvFile(Strict):
    schema_version: int
    sandbox_profiles: dict[Name, SandboxProfile] = Field(default_factory=dict[str, SandboxProfile])
    sandboxes: dict[Name, SandboxInstance] = Field(default_factory=dict[str, SandboxInstance])
    """Shared instances only. Every agent without `sandbox:` gets a private one."""
    canaries: tuple[CanaryDef, ...] = ()

    @model_validator(mode="after")
    def _unique_canaries(self) -> Self:
        _no_duplicates("canary", [c.id for c in self.canaries])
        _no_duplicates("canary path", [f"{c.sandbox}:{c.path}" for c in self.canaries])
        return self

    @model_validator(mode="after")
    def _profiles_exist(self) -> Self:
        for name, instance in self.sandboxes.items():
            if instance.profile not in self.sandbox_profiles:
                raise ValueError(
                    f"sandbox `{name}` uses unknown profile `{instance.profile}`. "
                    f"Profiles: {', '.join(self.sandbox_profiles) or 'none'}."
                )
        return self


def _no_duplicates(what: str, values: list[str]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"{what} `{value}` appears twice. Each must be unique.")
        seen.add(value)
