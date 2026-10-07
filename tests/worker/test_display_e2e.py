"""`browser` and `computer` through the whole platform: sandboxd starts the display image's
screen, screenshots reach the model inline, the export embeds them, and a fork after the first
display call is refused. Needs the display image (deploy/images/display) on the docker host."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import grpc
import pytest
import yaml
from inspect_ai.event import ModelEvent, ToolEvent
from inspect_ai.log import read_eval_log
from inspect_ai.model import ChatMessageTool, ContentImage

from swarmeval.analysis.exports import load_events_table
from swarmeval.control.bundles import pack
from swarmeval.events import export_key
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from tests.gateway.mock_backend import completion, tool_call
from tests.models import chosen
from tests.worker.conftest import Platform

IMAGE = os.environ.get("SWARMEVAL_IT_DISPLAY_IMAGE", "swarmeval/display:dev")

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0,
        reason=f"display image {IMAGE} is not on the docker host; build deploy/images/display",
    ),
]

PAGE = """<html><head><title>Shop</title></head><body>
<label>Name <input></label><button onclick="document.title='paid'">Pay</button></body></html>"""
CASE: dict[str, Any] = {
    "schema_version": 4,
    "id": "desktop",
    "workspace": "ws_e2e",
    "swarm": {
        "agents": [{"id": "a", "prompt": "a.md", "tools": ["browser", "computer"]}],
        "limits": {"max_turns": 6},
    },
    "task": {"input": "task.md"},
}
ENV: dict[str, Any] = {
    "schema_version": 2,
    "sandbox_profiles": {
        "default": {
            "image": IMAGE,
            "fs": [{"path": "/workspace"}],
            "limits": {"memory": "2gib", "pids": 512},
            "files": [{"from": "site", "to": "/workspace"}],
            "display": {"url": "file:///workspace/index.html"},
        }
    },
}


def bundle(root: Path) -> bytes:
    (root / "site").mkdir()
    files = {
        "case.yaml": yaml.safe_dump(CASE),
        "env.yaml": yaml.safe_dump(ENV),
        "a.md": "You are a.",
        "task.md": "Pay.",
        "site/index.html": PAGE,
    }
    for rel, text in files.items():
        (root / rel).write_text(text)
    return pack(root)


def policy(request: dict[str, Any]) -> dict[str, Any]:
    turns = sum(1 for m in request["messages"] if m["role"] == "assistant")
    if turns == 0:
        look = {"action": "snapshot", "screenshot": True}
        return completion("", tool_calls=[tool_call("browser", json.dumps(look), id="b1")])
    if turns == 1:
        snapshot = next(m["content"] for m in request["messages"] if m["role"] == "tool")
        ref = snapshot.split('button "Pay" [ref=')[1].split("]")[0]
        click = {"action": "click", "ref": ref}
        shot = {"action": "click", "coordinate": [10, 10]}
        return completion(
            "",
            tool_calls=[
                tool_call("browser", json.dumps(click), id="b2"),
                tool_call("computer", json.dumps(shot), id="c1"),
            ],
        )
    return completion("done")


async def test_an_agent_drives_the_display_and_sees_its_screenshots(
    platform: Platform, tmp_path: Path
) -> None:
    platform.backend.respond = policy
    (run_id,) = (
        await platform.control.SubmitRuns(
            pb.SubmitRunsRequest(models=chosen("mock-model"), case_bundle=bundle(tmp_path))
        )
    ).run_ids

    outcome = (await platform.worker.drain())[run_id]

    assert outcome.status == "done", outcome.error
    last = platform.backend.requests[-1]["messages"]
    roles = [m["role"] for m in last]
    assert roles == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert "Title: paid" in last[6]["content"]
    images = [p for m in (last[4], last[8]) for p in m["content"] if p["type"] == "image_url"]
    assert len(images) == 2, "the first snapshot's and the computer click's screenshots"
    assert all(p["image_url"]["url"].startswith("data:image/png;base64,iVBOR") for p in images)

    events = (await load_events_table(platform.store, run_id)).to_pylist()
    tools = [json.loads(e["payload"]) for e in events if e["type"] == "tool"]
    ours = [t["metadata"]["swarmeval"] for t in tools]
    assert [len(o.get("images", [])) for o in ours] == [1, 0, 1]
    assert [o["exec"]["processes"] for o in ours] == [[], [], []]
    assert ours[2]["images"][0]["width"] == 1024

    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "sample.eval"
        path.write_bytes(platform.store.get(export_key(run_id)))
        (sample,) = read_eval_log(path, resolve_attachments=True).samples or []
    shown = [e.result for e in sample.events if isinstance(e, ToolEvent)]
    assert isinstance(shown[0], list)
    assert isinstance(shown[0][1], ContentImage)
    final = [e for e in sample.events if isinstance(e, ModelEvent)][-1]
    seen = [
        c.image
        for m in final.input
        if isinstance(m, ChatMessageTool) and isinstance(m.content, list)
        for c in m.content
        if isinstance(c, ContentImage)
    ]
    assert len(seen) == 2
    assert all(i.startswith("data:image/png;base64,iVBOR") for i in seen)

    models = [e for e in events if e["type"] == "model"]
    before = await platform.control.ForkRun(
        pb.ForkRunRequest(run_id=run_id, at_event_id=models[0]["event_id"], actor="ada")
    )
    assert before.run.forked_from == run_id, "a fork before the first display call goes on"
    with pytest.raises(grpc.aio.AioRpcError) as err:
        await platform.control.ForkRun(
            pb.ForkRunRequest(run_id=run_id, at_event_id=models[-1]["event_id"], actor="ada")
        )
    assert err.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "used `browser`" in (err.value.details() or "")
    # The fork from before the first display call runs, here, so no later test's worker drains it.
    forked = (await platform.worker.drain())[before.run.run_id]
    assert forked.status == "done", forked.error
