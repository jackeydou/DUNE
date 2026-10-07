"""Channel interventions through the whole platform: a case whose channels paraphrase, drop,
delay, and inject, submitted through the Control API and run by the worker in sandboxd's
sandboxes against a scripted model behind model-gateway."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.analysis import trace
from swarmeval.analysis.exports import load_events_table
from swarmeval.control.bundles import pack
from swarmeval.db import deliveries
from swarmeval.gateway.bus.interventions import DEFAULT_PARAPHRASE_PROMPT
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from tests.gateway.mock_backend import completion, tool_call
from tests.models import chosen
from tests.worker.conftest import Platform

pytestmark = pytest.mark.docker

CASE: dict[str, Any] = {
    "schema_version": 4,
    "id": "interventions",
    "workspace": "ws_e2e",
    "swarm": {
        "agents": [
            {"id": "a", "prompt": "a.md", "tools": ["send_message"]},
            {"id": "b", "prompt": "b.md", "tools": ["send_message"]},
        ],
        "channels": [
            {
                "id": "ab",
                "members": ["a", "b"],
                "interventions": [
                    {"paraphrase": {"model": "qwen3-8b"}},
                    {"inject": {"at_turn": 1, "sender": "a", "content": "psst"}},
                ],
            },
            {"id": "side", "members": ["a", "b"], "interventions": [{"drop": {"p": 1.0}}]},
            {
                "id": "slow",
                "members": ["a", "b"],
                "interventions": [{"delay": {"turns": 1}}, "log"],
            },
        ],
        "limits": {"max_turns": 10},
    },
    "task": {"input": "task.md"},
}
ENV: dict[str, Any] = {
    "schema_version": 1,
    "sandbox_profiles": {"default": {"image": "busybox:latest", "fs": [{"path": "/workspace"}]}},
}


def write_case(root: Path) -> Path:
    files = {
        "case.yaml": yaml.safe_dump(CASE),
        "env.yaml": yaml.safe_dump(ENV),
        "a.md": "You are a.",
        "b.md": "You are b.",
        "task.md": "Agree on a price.",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def send(channel: str, content: str, id: str) -> dict[str, Any]:
    return tool_call("send_message", json.dumps({"channel": channel, "content": content}), id=id)


async def test_a_run_with_every_intervention_is_recorded_and_explained(
    platform: Platform, engine: AsyncEngine, tmp_path: Path
) -> None:
    backend = platform.backend
    # In call order: the injected message is paraphrased at a's first turn start, then a sends
    # on each channel, and the message on `ab` is paraphrased as it is routed.
    backend.reply(completion("A quiet word."))
    backend.reply(
        completion(
            "",
            tool_calls=[
                send("ab", "hello", "s1"),
                send("side", "lost", "s2"),
                send("slow", "later", "s3"),
            ],
        )
    )
    backend.reply(completion("Greetings."))
    backend.reply(completion("ok"))
    backend.reply(completion("done"))
    backend.reply(completion("thanks"))

    submitted = await platform.control.SubmitRuns(
        pb.SubmitRunsRequest(
            models=chosen("mock-model"), case_bundle=pack(write_case(tmp_path / "case"))
        )
    )
    (run_id,) = submitted.run_ids
    outcomes = await platform.worker.drain()

    assert outcomes[run_id].status == "done", outcomes[run_id].error
    for i, content in ((0, "psst"), (2, "hello")):
        messages = backend.requests[i]["messages"]
        assert backend.requests[i]["model"] == "Org/Mock"
        assert messages == [
            {"role": "system", "content": DEFAULT_PARAPHRASE_PROMPT},
            {"role": "user", "content": content},
        ]
    stream = platform.control.StreamEvents(pb.StreamEventsRequest(run_id=run_id))
    events = [item async for item in stream]
    payloads = {e.event_id: json.loads(e.payload_json) for e in events}
    meta = {i: p["metadata"]["swarmeval"] for i, p in payloads.items()}
    sends = [e for e in events if e.type == "swarmeval.msg.send"]
    by_content = {payloads[e.event_id]["data"]["content"]: e for e in sends}
    assert set(by_content) == {"psst", "hello", "lost", "later"}
    assert meta[by_content["psst"].event_id]["extension"] == "ab.inject"
    # The rewrites are model calls under the paraphrase instance, each hanging off its send.
    rewrites = [e for e in events if e.type == "model" and meta[e.event_id]["extension"]]
    assert [(meta[e.event_id]["extension"], e.agent_id) for e in rewrites] == [
        ("ab.paraphrase", ""),
        ("ab.paraphrase", ""),
    ]
    assert [meta[e.event_id]["parent_id"] for e in rewrites] == [
        by_content["psst"].event_id,
        by_content["hello"].event_id,
    ]
    delivered = [
        payloads[e.event_id]["data"]["content"] for e in events if e.type == "swarmeval.msg.deliver"
    ]
    assert delivered == ["A quiet word.", "Greetings.", "later"]

    async with engine.connect() as conn:
        rows = await conn.execute(
            select(deliveries.c.msg_seq, deliveries.c.status, deliveries.c.due_turn).where(
                deliveries.c.run_id == run_id
            )
        )
        statuses = {seq: (status, due) for seq, status, due in rows}
    assert statuses == {
        by_content["psst"].seq: ("delivered", None),
        by_content["hello"].seq: ("delivered", None),
        by_content["lost"].seq: ("dropped", None),
        by_content["later"].seq: ("delivered", 2),
    }

    (check,) = [
        payloads[e.event_id]["data"] for e in events if e.type == "swarmeval.transcript_check"
    ]
    interventions = [e.event_id for e in events if e.type == "swarmeval.intervention"]
    assert (check["consistent"], check["mismatches"], check["deliveries"]) == (True, [], 3)
    assert sorted(check["interventions"]) == sorted(interventions)
    assert len(interventions) == 5  # the post, two rewrites, a drop, a delay

    rows_table = await load_events_table(platform.store, run_id)
    later = next(
        e for e in events if e.type == "swarmeval.msg.deliver" and "later" in e.payload_json
    )
    lines = trace.text(trace.trace(rows_table, later.event_id)).splitlines()
    assert "swarmeval.intervention" in lines[-2] and '"kind": "delay"' in lines[-2]
    assert "swarmeval.msg.send" in lines[-3]
