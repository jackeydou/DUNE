import hashlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import grpc
import pytest

from swarmeval.core.models import SandboxProfile
from swarmeval.proto.swarmeval.sandbox.v1 import sandbox_pb2 as pb
from swarmeval.proto.swarmeval.sandbox.v1.sandbox_pb2_grpc import (
    SandboxServiceServicer,
    add_SandboxServiceServicer_to_server,
)
from swarmeval.runtime.messages import ToolCall
from swarmeval.runtime.records import Exec
from swarmeval.runtime.tools import SHELL, exec_output
from swarmeval.sandbox import RunSandboxes, SandboxdError

Context = grpc.aio.ServicerContext[Any, Any]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def header_item(**fields: Any) -> pb.ExecResponse:
    return pb.ExecResponse(header=pb.ExecHeader(**fields))


def blob_items(
    data: bytes, *, chunk: int = 4, announce: str | None = None
) -> list[pb.ExecResponse]:
    digest = announce or sha(data)
    pieces = [data[i : i + chunk] for i in range(0, len(data), chunk)] or [b""]
    return [
        pb.ExecResponse(blob=pb.BlobChunk(sha256=digest, data=p, last=i == len(pieces) - 1))
        for i, p in enumerate(pieces)
    ]


@dataclass
class FakeSandboxd(SandboxServiceServicer):
    exec_items: list[pb.ExecResponse] = field(default_factory=list[pb.ExecResponse])
    exec_error: grpc.StatusCode | None = None
    requests: list[Any] = field(default_factory=list[Any])

    async def CreateRun(
        self, request: pb.CreateRunRequest, context: Context
    ) -> pb.CreateRunResponse:
        self.requests.append(request)
        return pb.CreateRunResponse()

    async def CreateSandbox(
        self, request: pb.CreateSandboxRequest, context: Context
    ) -> pb.CreateSandboxResponse:
        self.requests.append(request)
        return pb.CreateSandboxResponse(runtime="runc")

    async def Exec(
        self, request: pb.ExecRequest, context: Context
    ) -> AsyncIterator[pb.ExecResponse]:
        self.requests.append(request)
        if self.exec_error is not None:
            await context.abort(self.exec_error, "sandbox box_a of run run_1 not found")
        for item in self.exec_items:
            yield item

    async def ReadFile(self, request: pb.ReadFileRequest, context: Context) -> pb.ReadFileResponse:
        self.requests.append(request)
        return pb.ReadFileResponse(content=b"abc", size=10, truncated=True)

    async def FinalDiff(
        self, request: pb.FinalDiffRequest, context: Context
    ) -> AsyncIterator[pb.FinalDiffResponse]:
        self.requests.append(request)
        change = pb.FsChange(
            path="/workspace/late.txt",
            op=pb.FsChange.OP_CREATE,
            kind=pb.FsChange.KIND_FILE,
            after_sha256=sha(b"late"),
            attribution=pb.FsChange.ATTRIBUTION_AMBIGUOUS,
            content=True,
        )
        yield pb.FinalDiffResponse(changes=pb.SandboxChanges(sandbox_id="box_a", changes=[change]))
        yield pb.FinalDiffResponse(blob=pb.BlobChunk(sha256=sha(b"late"), data=b"late", last=True))

    async def DestroyRun(
        self, request: pb.DestroyRunRequest, context: Context
    ) -> pb.DestroyRunResponse:
        self.requests.append(request)
        return pb.DestroyRunResponse()


@dataclass
class MemoryBlobs:
    stored: dict[str, bytes] = field(default_factory=dict[str, bytes])

    async def put(self, sha256: str, data: bytes) -> None:
        self.stored[sha256] = data


@dataclass
class Rig:
    server: FakeSandboxd
    blobs: MemoryBlobs
    client: RunSandboxes


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    fake = FakeSandboxd()
    server = grpc.aio.server()
    add_SandboxServiceServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    blobs = MemoryBlobs()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        yield Rig(fake, blobs, RunSandboxes(channel, "run_1", blobs))
    await server.stop(None)


LS = Exec(argv=("sh", "-c", "ls"), cwd="/workspace", timeout_s=5)


async def test_exec_sends_the_call_and_maps_the_header(rig: Rig) -> None:
    rig.server.exec_items = [
        header_item(
            exit_code=0,
            stdout=pb.Output(inline=b"a\x00b\xff", size=4),
            stderr=pb.Output(inline=b"", size=0),
            changes=[
                pb.FsChange(
                    path="/workspace/tests/t.py",
                    op=pb.FsChange.OP_MODIFY,
                    kind=pb.FsChange.KIND_FILE,
                    uid=1000,
                    mode=0o644,
                    size=3,
                    before_sha256="aa",
                    after_sha256="bb",
                    protected=True,
                    attribution=pb.FsChange.ATTRIBUTION_CALL,
                )
            ],
            processes=[pb.Process(pid=9, ppid=1, user="root", cmdline="sleep 100")],
        )
    ]

    result = await rig.client.exec("box_a", "qa", LS, call_id="call_1")

    (request,) = rig.server.requests
    assert (request.run_id, request.sandbox_id, request.call_id) == ("run_1", "box_a", "call_1")
    assert (list(request.argv), request.cwd, request.user) == (
        ["sh", "-c", "ls"],
        "/workspace",
        "qa",
    )
    assert request.timeout.seconds == 5
    assert result.stdout == "a�b�"
    (change,) = result.fs_changes
    assert (change.op, change.protected, change.before_sha256, change.mode) == (
        "modify",
        True,
        "aa",
        0o644,
    )
    assert result.processes[0].cmdline == "sleep 100"
    assert result.stdout_truncated is None


async def test_long_output_and_file_contents_go_to_the_blob_store(rig: Rig) -> None:
    full = b"0123456789" * 3
    content = b"new file content"
    rig.server.exec_items = [
        header_item(
            exit_code=0,
            stdout=pb.Output(inline=full[:8], size=len(full), blob_sha256=sha(full)),
            changes=[
                pb.FsChange(
                    path="/workspace/x",
                    op=pb.FsChange.OP_CREATE,
                    kind=pb.FsChange.KIND_FILE,
                    after_sha256=sha(content),
                    attribution=pb.FsChange.ATTRIBUTION_CALL,
                    content=True,
                )
            ],
        ),
        *blob_items(full),
        *blob_items(content),
    ]

    result = await rig.client.exec("box_a", None, LS, call_id="call_1")

    assert rig.blobs.stored == {sha(full): full, sha(content): content}
    assert result.stdout_truncated is not None
    assert (result.stdout_truncated.size, result.stdout_truncated.sha256) == (30, sha(full))
    assert result.fs_changes[0].content_stored
    shown = exec_output(ToolCall(id="c", name="shell", arguments="{}"), result)
    assert shown.content.startswith("01234567\n[stdout truncated: the command wrote 30 bytes]")


@pytest.mark.parametrize(
    ("items", "message"),
    [
        (
            lambda: [
                header_item(stdout=pb.Output(blob_sha256=sha(b"x"))),
                *blob_items(b"y", announce=sha(b"x")),
            ],
            "hashes to",
        ),
        (lambda: [header_item(stdout=pb.Output(blob_sha256=sha(b"x")))], "blobs missing"),
        (lambda: [header_item(), *blob_items(b"stray")], "not named by any output"),
        (list[pb.ExecResponse], "without a header"),
        (lambda: [header_item(), header_item()], "unexpected `header`"),
    ],
)
async def test_a_stream_that_breaks_the_contract_is_refused(
    rig: Rig, items: Callable[[], list[pb.ExecResponse]], message: str
) -> None:
    rig.server.exec_items = items()

    with pytest.raises(SandboxdError, match=message) as info:
        await rig.client.exec("box_a", None, LS, call_id="call_1")

    assert info.value.code is None
    assert rig.blobs.stored == {}


async def test_rpc_errors_name_the_run_sandbox_and_status(rig: Rig) -> None:
    rig.server.exec_error = grpc.StatusCode.NOT_FOUND

    with pytest.raises(SandboxdError) as info:
        await rig.client.exec("box_a", None, LS, call_id="call_1")

    assert info.value.code == grpc.StatusCode.NOT_FOUND
    assert "sandboxd Exec for run `run_1` sandbox `box_a` failed: NOT_FOUND" in str(info.value)
    assert "forgets its sandboxes when it restarts" in str(info.value)


async def test_create_maps_the_profile(rig: Rig) -> None:
    profile = SandboxProfile.model_validate(
        {
            "image": "busybox:latest",
            "fs": [
                {"path": "/workspace"},
                {"path": "/workspace/tests", "mode": "ro", "protected": True},
            ],
            "limits": {"cpu": 1.5, "memory": "1gib", "pids": 64},
        }
    )

    await rig.client.create_run(["box_a", "box_b"])
    runtime = await rig.client.create("box_a", profile, users=["qa", "dev"])

    run, request = rig.server.requests
    assert (run.run_id, list(run.sandbox_ids)) == ("run_1", ["box_a", "box_b"])
    assert runtime == "runc"
    assert list(request.users) == ["qa", "dev"]
    assert request.image == "busybox:latest"
    assert [(m.path, m.read_only, m.protected) for m in request.mounts] == [
        ("/workspace", False, False),
        ("/workspace/tests", True, True),
    ]
    assert (request.resources.cpus, request.resources.memory_bytes) == (1.5, 2**30)
    assert (request.resources.pids, request.resources.disk_bytes) == (64, 0)


async def test_read_file_final_diff_and_destroy(rig: Rig) -> None:
    file = await rig.client.read_file("box_a", "/workspace/answer.txt", max_bytes=3)
    changes = await rig.client.final_diff()
    await rig.client.destroy()

    assert (file.content, file.size, file.truncated) == (b"abc", 10, True)
    (late,) = changes["box_a"]
    assert (late.path, late.attribution, late.content_stored) == (
        "/workspace/late.txt",
        "ambiguous",
        True,
    )
    assert rig.blobs.stored == {sha(b"late"): b"late"}
    assert [type(r).__name__ for r in rig.server.requests] == [
        "ReadFileRequest",
        "FinalDiffRequest",
        "DestroyRunRequest",
    ]


def test_shell_runs_its_command_with_sh_and_the_given_timeout() -> None:
    args = SHELL.args.model_validate_json('{"cmd": "echo hi", "timeout_s": 5}')

    assert SHELL.build(args) == Exec(argv=("sh", "-c", "echo hi"), timeout_s=5)
