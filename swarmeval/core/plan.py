"""From a loaded variant to what the runtime drives: one `RunSpec` per run."""

from swarmeval.core.loader import Variant
from swarmeval.gateway.bus import ChannelSpec
from swarmeval.runtime import AgentSpec, Limits, RunSpec


def run_spec(variant: Variant, *, run_id: str, seed: int) -> RunSpec:
    """Raises `ValueError` for a variant whose case was loaded without its models chosen."""
    swarm = variant.case.swarm
    missing = [s for s in variant.case.slots if s not in variant.models]
    if missing:
        raise ValueError(
            f"variant {variant.index} of case `{variant.case.id}` has no model for slots "
            f"{', '.join(missing)}. Load the case with its models (`choose_models`) to run it."
        )
    agents = tuple(
        AgentSpec(
            id=agent.id,
            model=variant.models[agent.model_slot],
            system_prompt=variant.prompts[agent.id].system,
            task=variant.prompts[agent.id].task,
            tools=agent.tools,
            sandbox_id=variant.sandbox_of(agent.id).id,
            os_user=agent.os_user,
            temperature=agent.sampling.temperature,
            top_p=agent.sampling.top_p,
            max_output_tokens=agent.sampling.max_output_tokens,
            seed=agent.sampling.seed,
        )
        for agent in swarm.agents
    )
    limits = Limits(
        max_turns=swarm.limits.max_turns,
        max_tokens=swarm.limits.max_tokens,
        wall_clock_s=swarm.limits.wall_clock,
    )
    channels = tuple(ChannelSpec(id=c.id, members=c.members) for c in swarm.channels)
    return RunSpec(
        run_id=run_id,
        seed=seed,
        agents=agents,
        limits=limits,
        channels=channels,
        turn_policy=swarm.turn_policy,
    )
