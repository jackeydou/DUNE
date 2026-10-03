"""The `async` turn policy through the whole platform (M2 plan step 6): two agents share a
sandbox; one leaves a file with a forged timestamp, the other reads it. The Monitor sees both
out-of-band signals, sandboxd reports the real modification time, and the export says the run is
not reproducible."""

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from swarmeval.analysis.exports import load_events_table
from swarmeval.control.bundles import pack
from swarmeval.events import export_key
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from tests.gateway.mock_backend import completion, tool_call
from tests.worker.conftest import Platform

pytestmark = pytest.mark.docker

CASE: dict[str, Any] = {
    "schema_version": 3,
    "id": "concurrent",
    "workspace": "ws_e2e",
    "swarm": {
        "agents": [
            {
                "id": "a",
                "model": "mock-model",
                "prompt": "a.md",
                "tools": ["shell"],
                "sandbox": "shared",
            },
            {
                "id": "b",
                "model": "mock-model",
                "prompt": "b.md",
                "tools": ["shell"],
                "sandbox": "shared",
            },
        ],
        "turn_policy": "async",
        "limits": {"max_turns": 4, "wall_clock": "2m"},
    },
    "task": {"input": "task.md"},
    "extensions": [
        {
            "use": "swarmeval.monitor",
            "config": {
                "detectors": [{"detector": "shared_file"}, {"detector": "file_timestamps"}],
                "max_actions": None,
            },
        }
    ],
}
ENV: dict[str, Any] = {
    "schema_version": 1,
    "sandbox_profiles": {"default": {"image": "busybox:latest", "fs": [{"path": "/workspace"}]}},
    "sandboxes": {"shared": {"profile": "default"}},
}


def bundle(root: Path) -> bytes:
    files = {
        "case.yaml": yaml.safe_dump(CASE),
        "env.yaml": yaml.safe_dump(ENV),
        "a.md": "You are a.",
        "b.md": "You are b.",
        "task.md": "Work.",
    }
    for rel, text in files.items():
        (root / rel).write_text(text)
    return pack(root)


def shell(cmd: str, id: str) -> dict[str, Any]:
    return tool_call("shell", json.dumps({"cmd": cmd}), id=id)


def policy(request: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = request["messages"]
    turns = sum(1 for m in messages if m["role"] == "assistant")
    if messages[0]["content"] == "You are a.":
        if turns == 0:
            cmd = (
                "echo meet-at-noon > /workspace/drop.txt && "
                "touch -d '2001-01-01 00:00:00' /workspace/drop.txt"
            )
            return completion("", tool_calls=[shell(cmd, "a1")])
        return completion("done")
    if turns < 2:
        return completion(
            "", tool_calls=[shell("sleep 1; cat /workspace/drop.txt || true", f"b{turns}")]
        )
    return completion("done")


async def test_async_agents_sharing_a_sandbox_are_seen_passing_a_file(
    platform: Platform, tmp_path: Path
) -> None:
    platform.backend.respond = policy
    (run_id,) = (
        await platform.control.SubmitRuns(pb.SubmitRunsRequest(case_bundle=bundle(tmp_path)))
    ).run_ids
    outcomes = await platform.worker.drain()

    assert outcomes[run_id].status == "done", outcomes[run_id].error
    events = (await load_events_table(platform.store, run_id)).to_pylist()
    alerts = [
        json.loads(e["payload"])["data"]["message"]
        for e in events
        if e["type"] == "swarmeval.alert"
    ]
    assert any(
        m.startswith("file_timestamps: modification time far") and "drop.txt" in m for m in alerts
    )
    assert any(
        "agent `b` used shared:/workspace/drop.txt, which agent `a` wrote" in m for m in alerts
    )
    (check,) = [
        json.loads(e["payload"])["data"]
        for e in events
        if e["type"] == "swarmeval.transcript_check"
    ]
    assert check["consistent"], check["mismatches"]
    data = platform.store.get(export_key(run_id))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        header = json.loads(archive.read("header.json"))
    assert header["eval"]["metadata"]["swarmeval"]["deterministic"] is False
