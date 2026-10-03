"""The isolation probes' scripts in real sandboxes, on the images the docs promise they work on.

Builds sandboxd from `go/` and needs `busybox:latest` and `python:3.12-slim` on the docker host.
"""

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

import grpc
import pytest

from swarmeval.core.models import SandboxProfile
from swarmeval.runtime.ports import SandboxExecutor
from swarmeval.runtime.records import Exec, ExecResult, IsolationProbeRecord, LifecycleRecord
from swarmeval.runtime.writer import RunWriter
from swarmeval.sandbox import RunSandboxes
from swarmeval.worker.probes import IsolationError, ProbeSandbox, check_isolation
from tests.runtime.fakes import FakeStore
from tests.sandbox.test_client import MemoryBlobs

pytestmark = pytest.mark.docker

MOUNTS = [
    {"path": "/workspace"},
    {"path": "/workspace/tests", "mode": "ro", "protected": True},
]


def profile(image: str) -> SandboxProfile:
    return SandboxProfile.model_validate(
        {"image": image, "fs": MOUNTS, "limits": {"memory": "256mib", "pids": 128}}
    )


def target(sandbox_id: str, hostname: str) -> ProbeSandbox:
    return ProbeSandbox(
        sandbox_id=sandbox_id,
        names=(hostname, sandbox_id),
        key_paths=("/workspace", "/workspace/tests"),
        plant_dirs=("/workspace",),
    )


@pytest.fixture
async def client(sandboxd: str) -> AsyncIterator[RunSandboxes]:
    async with grpc.aio.insecure_channel(sandboxd) as channel:
        client = RunSandboxes(channel, f"probe_{uuid.uuid4().hex[:12]}", MemoryBlobs())
        try:
            yield client
        finally:
            await client.destroy()


async def create(client: RunSandboxes, image: str, *ids: str) -> dict[str, str]:
    await client.create_run(list(ids))
    hostnames: dict[str, str] = {}
    for sandbox_id in ids:
        token = uuid.uuid4().hex
        await client.create(
            sandbox_id,
            profile(image),
            env={"INSTANCE_ID": token},
            hostname=token,
            machine_id=token,
        )
        hostnames[sandbox_id] = token
    return hostnames


@pytest.mark.parametrize("image", ["busybox:latest", "python:3.12-slim"])
async def test_two_live_sandboxes_are_isolated_and_the_probe_leaves_nothing(
    client: RunSandboxes, image: str
) -> None:
    hostnames = await create(client, image, "a", "b")
    store = FakeStore()

    findings = await check_isolation(
        [target(s, h) for s, h in hostnames.items()], client, RunWriter(store)
    )

    outcomes = {
        (s, f.probe, f.peer): (f.outcome, f.detail) for s, fs in findings.items() for f in fs
    }
    assert len(outcomes) == 2 * (2 + 3), findings
    assert {o for o, _ in outcomes.values()} == {"isolated"}, outcomes
    steps = [
        (e.record.sandbox_id, e.record.step)
        for e in store.events
        if isinstance(e.record, IsolationProbeRecord)
    ]
    assert steps == [(s, step) for step in ("plant", "check", "clean") for s in ("a", "b")]
    # The first agent call sees no probe file and no probe process: its own write is its own.
    after = await client.exec(
        "a", None, Exec(argv=("sh", "-c", "echo x > /workspace/x"), timeout_s=10), call_id="c1"
    )
    assert after.background_changes == ()
    assert [(c.path, c.attribution) for c in after.fs_changes] == [("/workspace/x", "call")]


@dataclass
class Aliased:
    """Runs one sandbox's commands in another, standing in for two sandboxes that share
    everything."""

    client: SandboxExecutor
    aliases: Mapping[str, str]

    async def exec(
        self, sandbox_id: str, os_user: str | None, command: Exec, *, call_id: str
    ) -> ExecResult:
        return await self.client.exec(
            self.aliases.get(sandbox_id, sandbox_id), os_user, command, call_id=call_id
        )


async def test_two_sandboxes_that_share_a_container_fail_the_check(client: RunSandboxes) -> None:
    hostnames = await create(client, "busybox:latest", "a")
    store = FakeStore()

    with pytest.raises(IsolationError) as info:
        await check_isolation(
            [target("a", hostnames["a"]), target("twin", hostnames["a"])],
            Aliased(client, {"twin": "a"}),
            RunWriter(store),
        )

    message = str(info.value)
    assert "sandbox `a` and sandbox `twin`: proc got through" in message
    assert "sandbox `twin` and sandbox `a`: shared_path got through" in message
    assert "/workspace/.isolation-probe-" in message
    (failed,) = [e.record for e in store.events if isinstance(e.record, LifecycleRecord)]
    assert failed.status == "failed"
