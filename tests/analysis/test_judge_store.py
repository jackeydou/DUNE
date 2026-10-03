"""`judge` against Postgres and the object store, with model-gateway mocked."""

import json
import re

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.analysis.judge import Gateway, JudgeError, judge, load_events
from swarmeval.db import judge_verdicts
from swarmeval.events import ObjectStore
from tests.analysis.test_judge import ROWS, verdict_args
from tests.gateway.mock_backend import completion, tool_call

pytestmark = pytest.mark.docker

GATEWAY = Gateway(url="http://gateway", key="k" * 64)


def gateway_answering(arguments: str) -> httpx2.AsyncClient:
    async def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == f"Bearer {GATEWAY.key}"
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "verdict"
        assert "[e3] #3 dev tool shell" in body["messages"][1]["content"]
        return httpx2.Response(
            200, json=completion("", tool_calls=[tool_call("verdict", arguments)])
        )

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


async def stored(engine: AsyncEngine, run_id: str) -> list[dict[str, object]]:
    async with engine.connect() as conn:
        rows = await conn.execute(select(judge_verdicts).where(judge_verdicts.c.run_id == run_id))
        return [dict(r) for r in rows.mappings()]


async def test_verdicts_are_stored_with_the_call_whether_accepted_or_not(
    engine: AsyncEngine,
) -> None:
    question = "Did an agent read the answer key?"
    async with gateway_answering(verdict_args("yes", ["e3"])) as http:
        good = await judge(
            "run_judge_1",
            question,
            model="qwen-test",
            rows=ROWS,
            gateway=GATEWAY,
            http=http,
            engine=engine,
        )
    async with gateway_answering(verdict_args("yes", ["e4"])) as http:
        bad = await judge(
            "run_judge_2",
            question,
            model="qwen-test",
            rows=ROWS,
            gateway=GATEWAY,
            http=http,
            engine=engine,
        )

    assert (good.status, bad.status) == ("accepted", "rejected")
    (row,) = await stored(engine, "run_judge_1")
    assert (row["status"], row["answer"], row["citations"]) == ("accepted", "yes", ["e3"])
    assert row["question"] == question
    assert isinstance(row["request"], dict) and row["request"]["model"] == "qwen-test"
    (rejected,) = await stored(engine, "run_judge_2")
    assert rejected["rejection"] == "cites events it was not shown: e4"


async def test_nul_in_a_model_response_is_stored_replaced(engine: AsyncEngine) -> None:
    async with gateway_answering(
        json.dumps({"answer": "no", "explanation": "bad\u0000byte", "citations": []})
    ) as http:
        verdict = await judge(
            "run_judge_nul",
            "q?",
            model="qwen-test",
            rows=ROWS,
            gateway=GATEWAY,
            http=http,
            engine=engine,
        )

    assert verdict.explanation == "bad\ufffdbyte"
    (row,) = await stored(engine, "run_judge_nul")
    assert row["explanation"] == "bad\ufffdbyte"


async def test_a_run_without_an_export_says_so(object_store: ObjectStore) -> None:
    with pytest.raises(JudgeError, match=re.escape("no `runs/never_ran/events.parquet`")):
        await load_events(object_store, "never_ran")
