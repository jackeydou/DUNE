"""The Monitor through the whole platform: a hidden character in a message is an alert that
`trace` follows back to the message, and the run pauses until `ResumeRun` (M2 plan step 4's
exit condition)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import grpc
import pytest
import yaml

from swarmeval.analysis import trace
from swarmeval.analysis.exports import load_events_table
from swarmeval.control.bundles import pack
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from tests.gateway.mock_backend import completion, tool_call
from tests.models import chosen
from tests.worker.conftest import Platform

pytestmark = pytest.mark.docker

CASE: dict[str, Any] = {
    "schema_version": 4,
    "id": "monitored",
    "workspace": "ws_e2e",
    "swarm": {
        "agents": [
            {"id": "a", "prompt": "a.md", "tools": ["send_message"]},
            {"id": "b", "prompt": "b.md", "tools": []},
        ],
        "channels": [{"id": "ab", "members": ["a", "b"]}],
        "limits": {"max_turns": 6},
    },
    "task": {"input": "task.md"},
    "extensions": [
        {
            "use": "swarmeval.monitor",
            "config": {"detectors": [{"detector": "zero_width"}], "on_hit": "pause"},
        }
    ],
}
ENV: dict[str, Any] = {
    "schema_version": 1,
    "sandbox_profiles": {"default": {"image": "busybox:latest", "fs": [{"path": "/workspace"}]}},
}


def bundle(root: Path) -> bytes:
    files = {
        "case.yaml": yaml.safe_dump(CASE),
        "env.yaml": yaml.safe_dump(ENV),
        "a.md": "You are a.",
        "b.md": "You are b.",
        "task.md": "Talk.",
    }
    for rel, text in files.items():
        (root / rel).write_text(text)
    return pack(root)


def script(platform: Platform) -> None:
    note = json.dumps({"channel": "ab", "content": "hold​ steady"})
    platform.backend.reply(completion("", tool_calls=[tool_call("send_message", note, id="s1")]))
    platform.backend.reply(completion("ok"))
    platform.backend.reply(completion("done"))


async def until(platform: Platform, run_id: str, status: str) -> None:
    current = ""
    for _ in range(600):
        current = (await platform.control.GetRun(pb.GetRunRequest(run_id=run_id))).run.status
        if current == status:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} never became {status}; it is {current}")


async def test_an_alert_pauses_the_run_and_traces_back_to_the_message(
    platform: Platform, tmp_path: Path
) -> None:
    script(platform)
    (run_id,) = (
        await platform.control.SubmitRuns(
            pb.SubmitRunsRequest(models=chosen("mock-model"), case_bundle=bundle(tmp_path))
        )
    ).run_ids
    drain = asyncio.create_task(platform.worker.drain())

    await until(platform, run_id, "paused")
    resumed = await platform.control.ResumeRun(pb.ResumeRunRequest(run_id=run_id))
    assert resumed.run.status == "running"
    outcomes = await drain

    assert outcomes[run_id].status == "done", outcomes[run_id].error
    with pytest.raises(grpc.aio.AioRpcError) as err:
        await platform.control.ResumeRun(pb.ResumeRunRequest(run_id=run_id))
    assert err.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    table = await load_events_table(platform.store, run_id)
    events = table.to_pylist()
    types = [e["type"] for e in events]
    (alert,) = [e for e in events if e["type"] == "swarmeval.alert"]
    lifecycles = [
        json.loads(e["payload"])["data"]["status"]
        for e in events
        if e["type"] == "swarmeval.lifecycle"
    ]
    assert lifecycles == ["started", "paused", "resumed", "finished"]
    paused = next(
        i
        for i, e in enumerate(events)
        if e["type"] == "swarmeval.lifecycle"
        and json.loads(e["payload"])["data"]["status"] == "paused"
    )
    assert types.index("swarmeval.alert") < paused
    lines = trace.text(trace.trace(table, alert["event_id"])).splitlines()
    assert "swarmeval.alert" in lines[-1]
    assert "swarmeval.msg.send" in lines[-2] and "hold" in lines[-2]
    assert lines[-3].split()[2] == "a"  # the model call that sent it, by agent a


async def test_a_cancel_while_paused_stops_the_run(platform: Platform, tmp_path: Path) -> None:
    script(platform)
    (run_id,) = (
        await platform.control.SubmitRuns(
            pb.SubmitRunsRequest(models=chosen("mock-model"), case_bundle=bundle(tmp_path))
        )
    ).run_ids
    drain = asyncio.create_task(platform.worker.drain())

    await until(platform, run_id, "paused")
    await platform.control.CancelRun(pb.CancelRunRequest(run_id=run_id))
    outcomes = await drain

    assert outcomes[run_id].status == "cancelled"
