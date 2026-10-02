"""The Python client against a real sandboxd on the local docker daemon.

Builds sandboxd from `go/`, so it needs the Go toolchain (`mise run test:docker` provides it) and
`busybox:latest` on the host.
"""

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import grpc
import pytest

from swarmeval.core.models import SandboxProfile
from swarmeval.runtime.records import Exec
from swarmeval.sandbox import RunSandboxes, SeedFile
from tests.sandbox.test_client import MemoryBlobs

pytestmark = pytest.mark.docker

REPO = Path(__file__).resolve().parents[2]
PROFILE = SandboxProfile.model_validate(
    {
        "image": "busybox:latest",
        "fs": [
            {"path": "/workspace"},
            {"path": "/workspace/tests", "mode": "ro", "protected": True},
        ],
        "limits": {"memory": "256mib", "pids": 128},
    }
)


@pytest.fixture
async def run(sandboxd: str) -> AsyncIterator[RunSandboxes]:
    blobs = MemoryBlobs()
    async with grpc.aio.insecure_channel(sandboxd) as channel:
        client = RunSandboxes(channel, f"py_{uuid.uuid4().hex[:12]}", blobs)
        try:
            yield client
        finally:
            await client.destroy()


async def test_two_agents_writes_in_a_shared_sandbox_are_attributed_per_call(
    run: RunSandboxes,
) -> None:
    await run.create_run(["team_box"])
    assert await run.create("team_box", PROFILE) in ("runc", "runsc")

    dev = await run.exec(
        "team_box",
        None,
        Exec(argv=("sh", "-c", "echo dev > /workspace/dev.txt"), timeout_s=10),
        call_id="dev_1",
    )
    qa = await run.exec(
        "team_box",
        None,
        Exec(argv=("sh", "-c", "echo qa > /workspace/qa.txt; echo done"), timeout_s=10),
        call_id="qa_1",
    )

    assert [(c.path, c.op, c.attribution) for c in dev.fs_changes] == [
        ("/workspace/dev.txt", "create", "call")
    ]
    assert [(c.path, c.op, c.attribution) for c in qa.fs_changes] == [
        ("/workspace/qa.txt", "create", "call")
    ]
    assert qa.stdout == "done\n"
    assert qa.fs_changes[0].after_sha256 == hashlib.sha256(b"qa\n").hexdigest()
    file = await run.read_file("team_box", "/workspace/dev.txt")
    assert file.content == b"dev\n"


async def test_seed_files_are_in_place_and_not_reported_as_changes(run: RunSandboxes) -> None:
    await run.create_run(["box"])
    await run.create(
        "box", PROFILE, [SeedFile(path="/workspace/keys/answers.json", content=b"CANARY-7")]
    )

    result = await run.exec(
        "box",
        None,
        Exec(argv=("sh", "-c", "cat /workspace/keys/answers.json"), timeout_s=10),
        call_id="c1",
    )

    assert result.stdout == "CANARY-7"
    assert result.fs_changes == () and result.background_changes == ()


async def test_background_writes_surface_in_the_final_diff(run: RunSandboxes) -> None:
    await run.create_run(["box"])
    await run.create("box", PROFILE)
    started = await run.exec(
        "box",
        None,
        Exec(
            argv=(
                "sh",
                "-c",
                "(sleep 1; echo late > /workspace/late.txt; sleep 60) >/dev/null 2>&1 &",
            ),
            timeout_s=10,
        ),
        call_id="c1",
    )
    assert started.processes, "the background subshell survives the call"
    await asyncio.sleep(2)

    changes = await run.final_diff()

    (late,) = changes["box"]
    assert (late.path, late.attribution, late.candidate_calls) == (
        "/workspace/late.txt",
        "ambiguous",
        ("c1",),
    )
