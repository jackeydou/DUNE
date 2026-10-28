"""Load case-local tools, real prompts, and channels into the scripted in-memory loop."""

import json
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from swarmeval.core import load_case
from swarmeval.gateway.bus import ChannelSpec
from swarmeval.runtime.extensions import Extension, ExtensionUse, case_resolver
from swarmeval.runtime.messages import ModelResponse, ToolCall
from swarmeval.runtime.specs import AgentSpec
from swarmeval.runtime.writer import RunWriter
from swarmeval.scorers import FinalStateScoring
from tests.runtime.fakes import Harness, call, harness
from tests.scorers.test_final_state import FakeScoringSandboxes

CASES = Path(__file__).parents[2] / "cases"


def local_extension(
    case: str, variant: int = 0, **config: JsonValue
) -> tuple[Extension[Any, Any], ExtensionUse]:
    loaded = load_case(CASES / case).variants[variant]
    use = loaded.extensions[0]
    ext = case_resolver(loaded.code, case_id=case, allowed=True)(use.use)
    return ext, use.model_copy(update={"use": ext.id, "config": {**use.config, **config}})


def scenario(
    case: str,
    scripts: dict[str, list[ModelResponse]],
    *,
    variant: int = 0,
    seed: int = 7,
    **config: JsonValue,
) -> Harness:
    loaded = load_case(CASES / case).variants[variant]
    return harness(
        tuple(
            AgentSpec(
                id=a.id,
                model="test-model",
                system_prompt=loaded.prompts[a.id].system,
                task=loaded.prompts[a.id].task,
                tools=a.tools,
                sandbox_id=None,
            )
            for a in loaded.case.swarm.agents
        ),
        scripts,
        extensions=[local_extension(case, variant, **config)],
        seed=seed,
        channels=tuple(ChannelSpec(id=c.id, members=c.members) for c in loaded.case.swarm.channels),
    )


def tool(name: str, *, id: str | None = None, **arguments: object) -> ToolCall:
    return call(name, json.dumps(arguments), id=id)


def messages(h: Harness, agent: str) -> list[str]:
    return [m.content for m in h.store.messages(agent) if m.role == "tool"]


async def scores(case: str, h: Harness) -> dict[str, int]:
    records = await FinalStateScoring(
        scorers=load_case(CASES / case).variants[0].case.scorers,
        scripts={},
        canaries=(),
        agent_sandboxes={a: None for a in h.store.generations},
        sandboxes=FakeScoringSandboxes(),
        writer=RunWriter(h.store),
    ).run(h.store.events)
    return {r.scorer: r.value for r in records}
