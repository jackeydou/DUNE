"""Runs whose agents have `sandbox: none` (spec/2026-10-07-optional-sandbox): a case with no
sandbox at all runs to the end against a sandboxd that is not there, and a case with some
sandboxes creates and checks only those."""

import io
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from inspect_ai.log import read_eval_log
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.analysis.exports import load_events_table
from swarmeval.control.bundles import pack
from swarmeval.events import ObjectStore, export_key
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from tests.gateway.mock_backend import completion, tool_call
from tests.models import chosen
from tests.worker.conftest import Platform, _platform  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.docker

NOWHERE = "127.0.0.1:9"
"""A sandboxd address nothing listens on: any call to it fails the run as `interrupted`."""


@pytest.fixture
async def no_sandboxd(
    postgres_url: str, engine: AsyncEngine, object_store: ObjectStore
) -> AsyncIterator[Platform]:
    async with _platform(postgres_url, engine, object_store, NOWHERE) as started:
        yield started


def write_case(root: Path, case: dict[str, Any], env: dict[str, Any] | None = None) -> Path:
    files = {
        "case.yaml": yaml.safe_dump(case),
        "prompts/a.md": "You are a.",
        "prompts/b.md": "You are b.",
        "task.md": "Agree on a plan.",
    }
    if env is not None:
        files["env.yaml"] = yaml.safe_dump(env)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def chat_case(**extra: Any) -> dict[str, Any]:
    return {
        "schema_version": 5,
        "id": "chat",
        "workspace": "ws_e2e",
        "swarm": {
            "agents": [
                {"id": "a", "prompt": "prompts/a.md", "tools": ["send_message"], "sandbox": "none"},
                {"id": "b", "prompt": "prompts/b.md", "tools": ["send_message"], "sandbox": "none"},
            ],
            "channels": [{"id": "dm", "members": ["a", "b"]}],
            "limits": {"max_turns": 10},
        },
        "task": {"input": "task.md"},
        "scorers": [
            {
                "id": "hidden",
                "type": "rule",
                "detect": {"detector": "zero_width", "roles": ["message"]},
                "meaning": "a message carried invisible characters",
            }
        ],
        **extra,
    }


def converse(platform: Platform, first_tool: str = "send_message", first_args: str = "") -> None:
    backend = platform.backend
    backend.reply(
        completion(
            "",
            tool_calls=[
                tool_call(first_tool, first_args or '{"channel": "dm", "content": "plan?"}')
            ],
        )
    )
    backend.reply(
        completion("", tool_calls=[tool_call("send_message", '{"channel": "dm", "content": "ok"}')])
    )
    backend.reply(completion("agreed"))
    backend.reply(completion("ok"))


async def test_a_case_without_sandboxes_runs_without_sandboxd(
    no_sandboxd: Platform, tmp_path: Path
) -> None:
    converse(no_sandboxd)

    submitted = await no_sandboxd.control.SubmitRuns(
        pb.SubmitRunsRequest(
            models=chosen("mock-model"), case_bundle=pack(write_case(tmp_path, chat_case()))
        )
    )
    (run_id,) = submitted.run_ids
    outcomes = await no_sandboxd.worker.drain()

    assert outcomes[run_id].status == "done", outcomes[run_id].error
    run = (await no_sandboxd.control.GetRun(pb.GetRunRequest(run_id=run_id))).run
    assert (run.status, run.isolation) == ("done", "")
    stream = no_sandboxd.control.StreamEvents(pb.StreamEventsRequest(run_id=run_id))
    payloads = [json.loads(e.payload_json) async for e in stream]
    assert not [p for p in payloads if p["metadata"]["swarmeval"].get("probe")]
    assert {p["metadata"]["swarmeval"]["sandbox_id"] for p in payloads} == {None}
    log = read_eval_log(io.BytesIO(no_sandboxd.store.get(export_key(run_id))))
    assert log.samples is not None and log.samples[0].scores is not None
    assert {name: s.value for name, s in log.samples[0].scores.items()} == {"hidden": 0}


async def test_an_extension_that_runs_a_command_fails_a_run_without_sandboxes(
    no_sandboxd: Platform, tmp_path: Path
) -> None:
    case = chat_case(
        extensions=[
            {
                "use": "swarmeval.env_state",
                "config": {"snapshots": [{"id": "ls", "sandbox": "a", "run": "ls"}]},
            }
        ]
    )
    submitted = await no_sandboxd.control.SubmitRuns(
        pb.SubmitRunsRequest(
            models=chosen("mock-model"), case_bundle=pack(write_case(tmp_path, case))
        )
    )
    (run_id,) = submitted.run_ids
    outcomes = await no_sandboxd.worker.drain()

    assert outcomes[run_id].status == "failed"
    error = outcomes[run_id].error or ""
    assert f"run `{run_id}` has no sandboxes" in error
    assert "`sh -c ls` cannot run in sandbox `a`" in error


async def test_a_mixed_case_creates_and_checks_only_the_sandboxes_agents_use(
    platform: Platform, tmp_path: Path
) -> None:
    converse(platform, "shell", json.dumps({"cmd": "echo hi"}))
    case = chat_case(scorers=[])
    a = case["swarm"]["agents"][0]
    del a["sandbox"]
    a["tools"] = ["shell", "send_message"]
    env = {"schema_version": 1, "sandbox_profiles": {"default": {"image": "busybox:latest"}}}

    submitted = await platform.control.SubmitRuns(
        pb.SubmitRunsRequest(
            models=chosen("mock-model"), case_bundle=pack(write_case(tmp_path, case, env))
        )
    )
    (run_id,) = submitted.run_ids
    outcomes = await platform.worker.drain()

    assert outcomes[run_id].status == "done", outcomes[run_id].error
    run = (await platform.control.GetRun(pb.GetRunRequest(run_id=run_id))).run
    assert run.isolation in ("runc", "runsc")
    stream = platform.control.StreamEvents(pb.StreamEventsRequest(run_id=run_id))
    payloads = [json.loads(e.payload_json) async for e in stream]
    probes = [p for p in payloads if p["metadata"]["swarmeval"].get("probe")]
    assert [p["metadata"]["swarmeval"]["probe"]["step"] for p in probes] == [
        "plant",
        "check",
        "clean",
    ]
    assert {p["metadata"]["swarmeval"]["sandbox_id"] for p in probes} == {"a"}
    tools = [p for p in payloads if p["event"] == "tool"]
    where = [
        (p["metadata"]["swarmeval"]["agent_id"], p["metadata"]["swarmeval"]["sandbox_id"])
        for p in tools
    ]
    assert where == [("a", "a"), ("b", None)]


async def test_a_fork_of_a_run_without_sandboxes_restores_nothing_and_runs(
    no_sandboxd: Platform, tmp_path: Path
) -> None:
    converse(no_sandboxd)
    (source_id,) = (
        await no_sandboxd.control.SubmitRuns(
            pb.SubmitRunsRequest(
                models=chosen("mock-model"), case_bundle=pack(write_case(tmp_path, chat_case()))
            )
        )
    ).run_ids
    assert (await no_sandboxd.worker.drain())[source_id].status == "done"
    source = (await load_events_table(no_sandboxd.store, source_id)).to_pylist()
    first_send = next(e for e in source if e["type"] == "swarmeval.msg.send")
    for _ in range(3):
        no_sandboxd.backend.reply(completion("ok"))

    forked = await no_sandboxd.control.ForkRun(
        pb.ForkRunRequest(run_id=source_id, at_event_id=first_send["event_id"])
    )
    outcomes = await no_sandboxd.worker.drain()

    fork_id = forked.run.run_id
    assert outcomes[fork_id].status == "done", outcomes[fork_id].error
    run = (await no_sandboxd.control.GetRun(pb.GetRunRequest(run_id=fork_id))).run
    assert (run.fidelity, run.isolation) == ("fs_restored", "")
