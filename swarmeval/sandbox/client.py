"""The worker's sandboxd client, for one run (docs/services/sandboxd.md).

sandboxd's responses come from inside a sandbox: output bytes, paths, and command lines are
whatever the agent made them. This module is the boundary that turns them into text the event
log can hash and store (valid UTF-8, no NUL), and that checks every blob against its hash before
it reaches the blob store.
"""

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import grpc
from google.protobuf.duration_pb2 import Duration

from swarmeval.core.models import SandboxProfile
from swarmeval.proto.swarmeval.sandbox.v1 import sandbox_pb2 as pb
from swarmeval.proto.swarmeval.sandbox.v1.sandbox_pb2_grpc import SandboxServiceStub
from swarmeval.runtime.records import Exec, ExecResult, FsChange, ProcessInfo, Truncated
from swarmeval.sandbox.blobs import BlobStore


class SandboxdError(Exception):
    """sandboxd refused or failed a request. `code` is the gRPC status, `None` when sandboxd
    answered but broke the protocol."""

    def __init__(self, message: str, code: grpc.StatusCode | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SeedFile:
    """Written into a key path when the sandbox is created, as part of its baseline."""

    path: str
    content: bytes
    mode: int = 0o644


@dataclass(frozen=True)
class FileContent:
    content: bytes
    size: int
    """Size of the file in the sandbox."""
    truncated: bool


_OPS: dict[int, Literal["create", "modify", "delete"]] = {
    pb.FsChange.OP_CREATE: "create",
    pb.FsChange.OP_MODIFY: "modify",
    pb.FsChange.OP_DELETE: "delete",
}
_KINDS: dict[int, Literal["file", "dir", "symlink", "other"]] = {
    pb.FsChange.KIND_FILE: "file",
    pb.FsChange.KIND_DIR: "dir",
    pb.FsChange.KIND_SYMLINK: "symlink",
    pb.FsChange.KIND_OTHER: "other",
}
_ATTRIBUTIONS: dict[int, Literal["call", "ambiguous"]] = {
    pb.FsChange.ATTRIBUTION_CALL: "call",
    pb.FsChange.ATTRIBUTION_AMBIGUOUS: "ambiguous",
}


class RunSandboxes:
    """sandboxd as seen by one run's worker. Implements the runtime's `SandboxExecutor`.

    Blobs a response carries are uploaded before the call returns, so the event that names them
    can be committed right after.
    """

    def __init__(
        self,
        channel: grpc.aio.Channel,
        run_id: str,
        blobs: BlobStore,
        *,
        exec_margin_s: float = 300.0,
    ) -> None:
        """`exec_margin_s` is added to each command's own timeout to get the RPC deadline. It
        covers the file walks and hashing sandboxd does around the command."""
        self._stub = SandboxServiceStub(channel)
        self._run_id = run_id
        self._blobs = blobs
        self._exec_margin_s = exec_margin_s

    async def create_run(self, sandbox_ids: Sequence[str]) -> None:
        """Creates the run's networks, one per sandbox. Comes before any `create`."""
        try:
            await self._stub.CreateRun(
                pb.CreateRunRequest(run_id=self._run_id, sandbox_ids=sandbox_ids)
            )
        except grpc.aio.AioRpcError as err:
            raise self._error("CreateRun", ", ".join(sandbox_ids), err) from err

    async def create(
        self,
        sandbox_id: str,
        profile: SandboxProfile,
        files: Sequence[SeedFile] = (),
        users: Sequence[str] = (),
    ) -> str:
        """Creates and starts one sandbox, adding `users` to its image. Returns the container
        runtime it got."""
        limits = profile.limits
        request = pb.CreateSandboxRequest(
            run_id=self._run_id,
            sandbox_id=sandbox_id,
            image=profile.image,
            mounts=[
                pb.Mount(path=m.path, read_only=m.mode == "ro", protected=m.protected)
                for m in profile.fs
            ],
            resources=pb.Resources(
                cpus=limits.cpu or 0,
                memory_bytes=limits.memory or 0,
                pids=limits.pids or 0,
                disk_bytes=limits.disk or 0,
            ),
            files=[pb.SeedFile(path=f.path, content=f.content, mode=f.mode) for f in files],
            users=users,
        )
        try:
            response = await self._stub.CreateSandbox(request)
        except grpc.aio.AioRpcError as err:
            raise self._error("CreateSandbox", sandbox_id, err) from err
        return response.runtime

    async def exec(
        self, sandbox_id: str, os_user: str | None, command: Exec, *, call_id: str
    ) -> ExecResult:
        request = pb.ExecRequest(
            run_id=self._run_id,
            sandbox_id=sandbox_id,
            call_id=call_id,
            argv=command.argv,
            cwd=command.cwd or "",
            user=os_user or "",
            timeout=_duration(command.timeout_s),
        )
        where = f"sandbox `{sandbox_id}` call `{call_id}`"
        stream = self._stub.Exec(request, timeout=command.timeout_s + self._exec_margin_s)
        header: pb.ExecHeader | None = None
        blobs = _BlobCollector(where)
        try:
            async for item in stream:
                match item.WhichOneof("item"):
                    case "header" if header is None:
                        header = item.header
                        blobs.expect(_expected_blobs(header))
                    case "blob" if header is not None:
                        blobs.add(item.blob)
                    case other:
                        raise SandboxdError(
                            f"sandboxd Exec for run `{self._run_id}` {where}: unexpected "
                            f"`{other}` item; the stream must carry one header, then blobs."
                        )
        except grpc.aio.AioRpcError as err:
            raise self._error("Exec", sandbox_id, err) from err
        if header is None:
            raise SandboxdError(
                f"sandboxd Exec for run `{self._run_id}` {where}: the stream ended without a "
                "header."
            )
        await self._store(blobs.finish())
        stdout, stdout_truncated = _output(header.stdout)
        stderr, stderr_truncated = _output(header.stderr)
        return ExecResult(
            exit_code=header.exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=header.timed_out,
            duration_s=header.duration.ToTimedelta().total_seconds(),
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            fs_changes=tuple(_change(c, where) for c in header.changes),
            background_changes=tuple(_change(c, where) for c in header.background_changes),
            processes=tuple(
                ProcessInfo(pid=p.pid, ppid=p.ppid, user=_clean(p.user), cmdline=_clean(p.cmdline))
                for p in header.processes
            ),
        )

    async def read_file(self, sandbox_id: str, path: str, *, max_bytes: int = 0) -> FileContent:
        """Reads a file without running anything in the sandbox. `max_bytes` of 0 means
        sandboxd's content limit."""
        request = pb.ReadFileRequest(
            run_id=self._run_id, sandbox_id=sandbox_id, path=path, max_bytes=max_bytes
        )
        try:
            response = await self._stub.ReadFile(request)
        except grpc.aio.AioRpcError as err:
            raise self._error("ReadFile", sandbox_id, err) from err
        return FileContent(
            content=response.content, size=response.size, truncated=response.truncated
        )

    async def final_diff(self) -> dict[str, tuple[FsChange, ...]]:
        """Changes since each sandbox's last call, all ambiguous, keyed by sandbox id."""
        stream = self._stub.FinalDiff(pb.FinalDiffRequest(run_id=self._run_id))
        where = "final diff"
        found: dict[str, tuple[FsChange, ...]] = {}
        blobs = _BlobCollector(where)
        try:
            async for item in stream:
                match item.WhichOneof("item"):
                    case "changes":
                        sandbox = item.changes
                        found[sandbox.sandbox_id] = tuple(
                            _change(c, where) for c in sandbox.changes
                        )
                        blobs.expect(c.after_sha256 for c in sandbox.changes if c.content)
                    case "blob":
                        blobs.add(item.blob)
                    case other:
                        raise SandboxdError(
                            f"sandboxd FinalDiff for run `{self._run_id}`: unexpected `{other}` "
                            "item."
                        )
        except grpc.aio.AioRpcError as err:
            raise self._error("FinalDiff", None, err) from err
        await self._store(blobs.finish())
        return found

    async def destroy(self) -> None:
        """Removes every container of the run and its state. Safe to call more than once."""
        try:
            await self._stub.DestroyRun(pb.DestroyRunRequest(run_id=self._run_id))
        except grpc.aio.AioRpcError as err:
            raise self._error("DestroyRun", None, err) from err

    async def _store(self, blobs: Mapping[str, bytes]) -> None:
        for sha256, data in blobs.items():
            await self._blobs.put(sha256, data)

    def _error(self, rpc: str, sandbox_id: str | None, err: grpc.aio.AioRpcError) -> SandboxdError:
        where = f"run `{self._run_id}`" + (f" sandbox `{sandbox_id}`" if sandbox_id else "")
        hint = {
            grpc.StatusCode.UNAVAILABLE: " Is sandboxd running and reachable from the worker?",
            grpc.StatusCode.NOT_FOUND: " sandboxd forgets its sandboxes when it restarts.",
        }.get(err.code(), "")
        return SandboxdError(
            f"sandboxd {rpc} for {where} failed: {err.code().name}: {err.details()}.{hint}",
            err.code(),
        )


class _BlobCollector:
    """Reassembles chunked blobs and checks each against its hash."""

    def __init__(self, where: str) -> None:
        self._where = where
        self._expected: set[str] = set()
        self._partial: dict[str, bytearray] = {}
        self._done: dict[str, bytes] = {}

    def expect(self, hashes: Iterable[str]) -> None:
        self._expected.update(hashes)

    def add(self, chunk: pb.BlobChunk) -> None:
        if chunk.sha256 not in self._expected:
            raise SandboxdError(
                f"sandboxd {self._where}: blob {chunk.sha256!r} is not named by any output or "
                "change."
            )
        buf = self._partial.setdefault(chunk.sha256, bytearray())
        buf.extend(chunk.data)
        if not chunk.last:
            return
        data = bytes(self._partial.pop(chunk.sha256))
        actual = hashlib.sha256(data).hexdigest()
        if actual != chunk.sha256:
            raise SandboxdError(
                f"sandboxd {self._where}: blob announced as {chunk.sha256} hashes to {actual}."
            )
        self._done[chunk.sha256] = data

    def finish(self) -> dict[str, bytes]:
        missing = self._expected - self._done.keys()
        if missing or self._partial:
            raise SandboxdError(
                f"sandboxd {self._where}: the stream ended with blobs missing "
                f"({', '.join(sorted(missing | self._partial.keys()))})."
            )
        return self._done


def _duration(seconds: float) -> Duration:
    duration = Duration()
    duration.FromTimedelta(timedelta(seconds=seconds))
    return duration


def _expected_blobs(header: pb.ExecHeader) -> list[str]:
    hashes = [o.blob_sha256 for o in (header.stdout, header.stderr) if o.blob_sha256]
    for change in (*header.changes, *header.background_changes):
        if change.content:
            hashes.append(change.after_sha256)
    return hashes


def _clean(text: str) -> str:
    """Postgres `jsonb` rejects NUL, so it never reaches an event."""
    return text.replace("\x00", "�")


def _text(data: bytes) -> str:
    return _clean(data.decode("utf-8", errors="replace"))


def _output(output: pb.Output) -> tuple[str, Truncated | None]:
    text = _text(output.inline)
    if not output.blob_sha256:
        return text, None
    return text, Truncated(size=output.size, sha256=output.blob_sha256, capped=output.capped)


def _change(change: pb.FsChange, where: str) -> FsChange:
    try:
        op = _OPS[change.op]
        kind = _KINDS[change.kind]
        attribution = _ATTRIBUTIONS[change.attribution]
    except KeyError as err:
        raise SandboxdError(
            f"sandboxd {where}: change to {change.path!r} has an unspecified enum value {err}."
        ) from err
    return FsChange(
        path=_clean(change.path),
        op=op,
        kind=kind,
        uid=change.uid,
        mode=change.mode,
        size=change.size,
        before_sha256=change.before_sha256 or None,
        after_sha256=change.after_sha256 or None,
        protected=change.protected,
        attribution=attribution,
        candidate_calls=tuple(change.candidate_calls),
        content_stored=change.content,
    )
