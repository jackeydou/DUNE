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
    PositiveFloat,
    PositiveInt,
    model_validator,
)

from swarmeval.runtime.extensions import ExtensionUse

SCHEMA_VERSIONS = frozenset({1})

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=63)]
"""Ids of cases, agents, channels, sandboxes, profiles, and variant axes. They end up in
container names, labels, and event fields, so they are kept to one safe alphabet."""

Workspace = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=63)]
UnixUser = Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_-]*$", max_length=32)]
Scalar = str | int | float | bool


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


class ChannelDef(Strict):
    id: Name
    members: Annotated[tuple[Name, ...], Field(min_length=2)]


class Limits(Strict):
    max_turns: PositiveInt | None = None
    max_tokens: Count | None = None


class SwarmDef(Strict):
    agents: Annotated[tuple[AgentDef, ...], Field(min_length=1)]
    channels: tuple[ChannelDef, ...] = ()
    turn_policy: Literal["round_robin"] = "round_robin"
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


class CaseFile(Strict):
    schema_version: int
    id: Name
    workspace: Workspace
    category: str | None = None
    description: str | None = None
    variants: dict[Name, Annotated[tuple[Scalar, ...], Field(min_length=1)]] = Field(
        default_factory=dict[str, tuple[Scalar, ...]]
    )
    epochs: PositiveInt = 1
    swarm: SwarmDef
    environment: RelPath = "env.yaml"
    task: TaskDef | None = None
    extensions: tuple[ExtensionUse, ...] = ()

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


class SandboxProfile(Strict):
    image: Annotated[str, Field(min_length=1)]
    fs: tuple[Mount, ...] = ()
    limits: ResourceLimits = ResourceLimits()

    @model_validator(mode="after")
    def _unique_mounts(self) -> Self:
        _no_duplicates("mount path", [m.path for m in self.fs])
        return self


class SandboxInstance(Strict):
    profile: Name


class EnvFile(Strict):
    schema_version: int
    sandbox_profiles: dict[Name, SandboxProfile] = Field(default_factory=dict[str, SandboxProfile])
    sandboxes: dict[Name, SandboxInstance] = Field(default_factory=dict[str, SandboxInstance])
    """Shared instances only. Every agent without `sandbox:` gets a private one."""

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
