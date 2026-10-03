"""What a run is: its agents, limits, channels, and canaries, and how it ended."""

from dataclasses import dataclass
from typing import Literal

from swarmeval.gateway.bus import ChannelSpec
from swarmeval.runtime.extensions.api import CanaryInfo, SandboxCanaryInfo
from swarmeval.runtime.messages import ChatMessage, SystemMessage, UserMessage


class RunConfigError(Exception):
    """The run's agents and tools do not fit together."""


@dataclass(frozen=True)
class AgentSpec:
    id: str
    model: str
    system_prompt: str
    task: str
    tools: tuple[str, ...] = ()
    sandbox_id: str | None = None
    os_user: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    seed: int | None = None


def initial_context(agent: AgentSpec) -> tuple[ChatMessage, ...]:
    """Generation 0 of an agent's context, before its first turn."""
    return (SystemMessage(content=agent.system_prompt), UserMessage(content=agent.task))


@dataclass(frozen=True)
class Limits:
    max_turns: int | None = None
    """Turns across all agents."""
    max_tokens: int | None = None
    """Tokens across all agents. Checked before each model call, so one call may overshoot."""


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    seed: int
    agents: tuple[AgentSpec, ...]
    limits: Limits = Limits()
    channels: tuple[ChannelSpec, ...] = ()
    canaries: tuple[CanaryInfo, ...] = ()
    sandbox_canaries: tuple[SandboxCanaryInfo, ...] = ()


@dataclass(frozen=True)
class RunOutcome:
    status: Literal["finished", "stopped", "limit"]
    reason: str | None
    turns: int
    tokens_used: int
