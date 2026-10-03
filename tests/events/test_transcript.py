"""The transcript check reading a run back from Postgres: the rows give the same answer as the
run in memory, and a context row changed after the fact is caught."""

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.db import messages
from swarmeval.events.transcript import load_transcript
from swarmeval.runtime.loop import initial_context
from swarmeval.runtime.tools import SHELL, tool_schema
from swarmeval.worker.transcript import check_transcript
from tests.events.test_store import run_loop, store_for
from tests.runtime.fakes import FakeStore, agent

pytestmark = pytest.mark.docker

PROMPTS = {a: initial_context(agent(a)) for a in ("a", "b")}
TOOLS = {"shell": tool_schema(SHELL)}


async def test_a_stored_run_reads_back_as_it_ran(engine: AsyncEngine, run_id: str) -> None:
    await run_loop(store_for(engine, run_id), run_id)
    fake = FakeStore()
    await run_loop(fake, "fake")

    transcript = await load_transcript(engine, run_id)

    assert [c.response for c in transcript.model_calls] == [
        e.record.response for e in fake.records("model") if e.record.kind == "model"
    ]
    assert transcript.contexts == {
        (a, gen): tuple(messages)
        for a, gens in fake.generations.items()
        for gen, messages in enumerate(gens)
    }
    result = check_transcript(transcript, prompts=PROMPTS, tools=TOOLS)
    assert result.mismatches == ()
    assert (result.requests, result.responses) == (3, 3)


async def test_a_context_row_changed_after_the_run_is_a_mismatch(
    engine: AsyncEngine, run_id: str
) -> None:
    await run_loop(store_for(engine, run_id), run_id)
    async with engine.begin() as conn:
        await conn.execute(
            update(messages)
            .where(messages.c.run_id == run_id, messages.c.agent_id == "a", messages.c.idx == 3)
            .values(message={"role": "tool", "tool_call_id": "call_shell", "content": "all green"})
        )

    transcript = await load_transcript(engine, run_id)
    result = check_transcript(transcript, prompts=PROMPTS, tools=TOOLS)

    (tool,) = transcript.tool_results
    second = [c for c in transcript.model_calls if c.agent_id == "a"][1]
    assert [(m.check, m.idx, m.event_id) for m in result.mismatches] == [
        ("context", 3, tool.event_id),
        ("request", 4, second.event_id),
    ]
    assert result.event_ids == (tool.event_id, second.event_id)
