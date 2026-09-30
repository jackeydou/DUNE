"""From a loaded variant to what the runtime drives: one `RunSpec` per run."""

from swarmeval.core.loader import Variant
from swarmeval.runtime import AgentSpec, Limits, RunSpec


def run_spec(variant: Variant, *, run_id: str, seed: int) -> RunSpec:
    swarm = variant.case.swarm
    agents = tuple(
        AgentSpec(
            id=agent.id,
            model=agent.model,
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
    limits = Limits(max_turns=swarm.limits.max_turns, max_tokens=swarm.limits.max_tokens)
    return RunSpec(run_id=run_id, seed=seed, agents=agents, limits=limits)
