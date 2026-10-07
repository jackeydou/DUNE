"""A variant's sandboxes: which instances its agents use, and what each is seeded with.

Checks here span `case.yaml` and `env.yaml`; the loader calls them once both are validated.
"""

import posixpath
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from swarmeval.core.errors import CaseError
from swarmeval.core.models import NO_SANDBOX, NO_SANDBOX_VERSION, CaseFile, EnvFile
from swarmeval.runtime.display import DISPLAY_TOOL_NAMES

Tree = Callable[[str, str], list[tuple[str, Path]]]
"""A case path and the field naming it → each file under it, relative to it, and its path."""

SEED_LIMIT = 1 << 20
"""sandboxd's limit on the files written into one sandbox at creation, canaries included."""


@dataclass(frozen=True)
class FileSeed:
    path: str
    content: bytes
    mode: int


@dataclass(frozen=True)
class SandboxPlan:
    id: str
    """Shared instances keep their declared name; a private one is named after its agent."""
    profile: str
    shared: bool
    agents: tuple[str, ...]


def check_crossing(
    where: str, scorer_id: str, sandboxes: Mapping[str, SandboxPlan], agents: int
) -> None:
    """`cross_sandbox` needs a sandbox and an agent outside it: either a second sandbox, or an
    agent with none."""
    if not sandboxes:
        raise CaseError(
            f"{where}: scorer `{scorer_id}` looks for information crossing between sandboxes, "
            "but no agent has a sandbox. Remove the scorer."
        )
    if len(sandboxes) == 1 and len(next(iter(sandboxes.values())).agents) == agents:
        raise CaseError(
            f"{where}: scorer `{scorer_id}` looks for information crossing between sandboxes, "
            f"but every agent uses sandbox `{next(iter(sandboxes))}`. Give agents their own "
            "sandboxes, or remove the scorer."
        )


def check_displays(
    case: CaseFile, env: EnvFile, sandboxes: Mapping[str, SandboxPlan], where: str
) -> None:
    """`browser` and `computer` drive a sandbox's display, so the agent's profile needs one. An
    agent with no sandbox cannot list them (`CaseFile`)."""
    tools = {a.id: a.tools for a in case.swarm.agents}
    for plan in sandboxes.values():
        if env.sandbox_profiles[plan.profile].display is not None:
            continue
        for agent_id in plan.agents:
            wanted = [t for t in tools[agent_id] if t in DISPLAY_TOOL_NAMES]
            if wanted:
                raise CaseError(
                    f"{where}: agent `{agent_id}` lists {', '.join(f'`{t}`' for t in wanted)}, "
                    f"but its sandbox `{plan.id}` has profile `{plan.profile}`, which has no "
                    "`display`. Add `display: {}` to the profile and use an image built from "
                    "swarmeval/display."
                )


def check_canaries(env: EnvFile, sandboxes: Mapping[str, SandboxPlan], where: str) -> None:
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


def plan_sandboxes(case: CaseFile, env: EnvFile, env_path: Path) -> dict[str, SandboxPlan]:
    agents_of: dict[str, list[str]] = {}
    profile_of: dict[str, str] = {}
    for agent in case.swarm.agents:
        if not case.has_sandbox(agent):
            continue
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
                    "Add a `default` profile, or set `sandbox_profile:` or `sandbox:` on it"
                    + (
                        ", or `sandbox: none` if it runs no commands."
                        if case.schema_version >= NO_SANDBOX_VERSION
                        else "."
                    )
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
        reserved = (
            f" `{NO_SANDBOX}` cannot be one: from case version {NO_SANDBOX_VERSION}, "
            f"`sandbox: {NO_SANDBOX}` means no sandbox. Rename it."
            if NO_SANDBOX in unused and case.schema_version >= NO_SANDBOX_VERSION
            else ""
        )
        raise CaseError(
            f"{env_path} declares shared sandboxes {', '.join(f'`{u}`' for u in unused)} that no "
            f"agent uses. Point an agent at each with `sandbox:`, or remove them.{reserved}"
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


def seed_files(
    env: EnvFile, sandboxes: Mapping[str, SandboxPlan], tree: Tree, where: str
) -> dict[str, tuple[FileSeed, ...]]:
    """The case files each sandbox's profile copies in. `tree` lists a case path's files."""
    by_profile: dict[str, tuple[FileSeed, ...]] = {}
    for name, profile in env.sandbox_profiles.items():
        mounts = [m.path for m in profile.fs]
        seeds: list[FileSeed] = []
        for i, copy in enumerate(profile.files):
            field = f"sandbox_profiles.{name}.files[{i}]"
            for rel, path in tree(copy.source, f"{field}.from"):
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
