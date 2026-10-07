"""The case library through the Control API: pushes, edits, conflicts, archiving, and runs
submitted by revision (M4 spec decision 6)."""

import asyncio
import io
import secrets
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import grpc
import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.bundles import MAX_BUNDLE_BYTES, bundle_hash, bundle_key, pack
from swarmeval.control.case_rpcs import MAX_NOTE_CHARS
from swarmeval.control.server import MAX_MESSAGE_BYTES
from swarmeval.db import case_revisions
from swarmeval.events import ObjectStore
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from tests.core.test_loader import base_case, write
from tests.models import chosen

if TYPE_CHECKING:
    from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import ControlServiceAsyncStub

pytestmark = pytest.mark.docker


@pytest.fixture
def workspace() -> str:
    """A workspace of the test's own in the shared database."""
    return f"ws-{secrets.token_hex(4)}"


def case_in(workspace: str, **changes: Any) -> dict[str, Any]:
    return {**base_case(), "workspace": workspace, **changes}


def bundle_of(tmp_path: Path, workspace: str, files: dict[str, str] | None = None) -> bytes:
    return pack(write(tmp_path, case_in(workspace), files=files))


def write_change(path: str, text: str) -> pb.FileChange:
    return pb.FileChange(path=path, content=text.encode())


async def refused(call: Any) -> tuple[grpc.StatusCode, str]:
    with pytest.raises(grpc.aio.AioRpcError) as info:
        await call
    return info.value.code(), str(info.value.details())


async def revision_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(case_revisions))).scalar_one()


async def test_a_push_makes_a_revision_only_when_the_bundle_changed(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    first = bundle_of(tmp_path / "1", workspace)
    second = bundle_of(tmp_path / "2", workspace, {"task.md": "Fix the other bug."})

    pushed = await control.PushCase(pb.PushCaseRequest(case_bundle=first, actor="ada", note="v1"))
    again = await control.PushCase(pb.PushCaseRequest(case_bundle=first, actor="bob"))
    changed = await control.PushCase(pb.PushCaseRequest(case_bundle=second, actor="bob"))
    back = await control.PushCase(pb.PushCaseRequest(case_bundle=first, actor="ada"))

    assert (pushed.created, again.created, changed.created, back.created) == (
        True,
        False,
        True,
        True,
    )
    assert again.revision == pushed.revision
    assert (pushed.revision.revision, pushed.revision.actor, pushed.revision.note) == (
        1,
        "ada",
        "v1",
    )
    assert pushed.revision.bundle_sha256 == bundle_hash(first)
    listed = await control.ListCaseRevisions(
        pb.ListCaseRevisionsRequest(workspace=workspace, case_id="demo")
    )
    assert [(r.revision, r.actor) for r in listed.revisions] == [(3, "ada"), (2, "bob"), (1, "ada")]
    assert listed.revisions[0].bundle_sha256 == listed.revisions[2].bundle_sha256
    case = (await control.GetCase(pb.GetCaseRequest(workspace=workspace, case_id="demo"))).case
    assert case.latest.revision == 3 and not case.HasField("archived_at")


def repacked(bundle: bytes) -> bytes:
    """The same files as another tool would archive them: another header format, times and
    owners set, no padding to a record."""
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(bundle)) as src,
        tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as dst,
    ):
        for member in src:
            member.mtime, member.uid, member.uname = 1_790_000_000, 501, "ada"
            dst.addfile(member, src.extractfile(member))
    return out.getvalue().rstrip(b"\0") + b"\0" * 1024


async def test_the_same_files_archived_by_another_tool_are_the_same_revision(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    bundle = bundle_of(tmp_path, workspace)
    other = repacked(bundle)
    assert other != bundle

    pushed = await control.PushCase(pb.PushCaseRequest(case_bundle=other))
    again = await control.PushCase(pb.PushCaseRequest(case_bundle=bundle))
    edited = await control.UpdateCaseFiles(
        pb.UpdateCaseFilesRequest(
            workspace=workspace,
            case_id="demo",
            base_revision=1,
            changes=[write_change("task.md", "Fix it twice.")],
        )
    )
    pulled = await control.GetCaseRevision(
        pb.GetCaseRevisionRequest(workspace=workspace, case_id="demo")
    )
    for f in pulled.files:
        path = tmp_path / "pulled" / f.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f.content)
    back = await control.PushCase(
        pb.PushCaseRequest(case_bundle=repacked(pack(tmp_path / "pulled")))
    )
    submitted = await control.SubmitRuns(
        pb.SubmitRunsRequest(models=chosen(), case_bundle=other, epochs=1)
    )

    assert (pushed.created, again.created) == (True, False)
    assert pushed.revision.bundle_sha256 == bundle_hash(bundle)
    # What an edit stored, pulled and pushed back unchanged, is still that revision.
    assert (edited.revision.revision, back.created, back.revision.revision) == (2, False, 2)
    assert submitted.case_revision == 3
    run = (await control.GetRun(pb.GetRunRequest(run_id=submitted.run_ids[0]))).run
    assert run.case_sha256 == bundle_hash(bundle)
    await control.CancelRun(pb.CancelRunRequest(run_id=run.run_id))


def test_a_bundle_of_the_limit_fits_the_control_apis_messages() -> None:
    bundle = bytes(MAX_BUNDLE_BYTES)
    push = pb.PushCaseRequest(case_bundle=bundle, actor="a" * 256, note="n" * MAX_NOTE_CHARS)
    suite = pb.SubmitSuiteRequest(
        suite_yaml="x" * (512 << 10), case_bundles={"p" * 4096: bundle}, actor="a" * 256
    )

    assert push.ByteSize() < MAX_MESSAGE_BYTES
    assert suite.ByteSize() < MAX_MESSAGE_BYTES


async def test_a_case_that_does_not_load_stores_nothing(
    control: "ControlServiceAsyncStub", engine: AsyncEngine, workspace: str, tmp_path: Path
) -> None:
    broken = case_in(workspace)
    broken["swarm"]["agents"][0]["prompt"] = "prompts/missing.md"
    before = await revision_count(engine)

    code, details = await refused(
        control.PushCase(pb.PushCaseRequest(case_bundle=pack(write(tmp_path, broken))))
    )

    assert code == grpc.StatusCode.INVALID_ARGUMENT and "prompts/missing.md" in details
    assert await revision_count(engine) == before
    code, _ = await refused(control.GetCase(pb.GetCaseRequest(workspace=workspace, case_id="demo")))
    assert code == grpc.StatusCode.NOT_FOUND


async def test_an_edit_makes_the_next_revision_from_its_base(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    await control.PushCase(pb.PushCaseRequest(case_bundle=bundle_of(tmp_path, workspace)))

    edited = await control.UpdateCaseFiles(
        pb.UpdateCaseFilesRequest(
            workspace=workspace,
            case_id="demo",
            base_revision=1,
            changes=[
                write_change("task.md", "Fix it twice."),
                write_change("notes/why.md", "because"),
                pb.FileChange(path="prompts/qa.md", delete=True),
                write_change("prompts/qa2.md", "You are qa."),
                write_change(
                    "case.yaml",
                    yaml.safe_dump(
                        case_in(
                            workspace,
                            swarm={
                                "agents": [
                                    {"id": "dev", "prompt": "prompts/dev.md"},
                                    {"id": "qa", "prompt": "prompts/qa2.md"},
                                ]
                            },
                        )
                    ),
                ),
            ],
            actor="ada",
            note="second prompt",
        )
    )

    assert edited.created and edited.revision.revision == 2
    assert (edited.revision.actor, edited.revision.note) == ("ada", "second prompt")
    got = await control.GetCaseRevision(
        pb.GetCaseRevisionRequest(workspace=workspace, case_id="demo")
    )
    files = {f.path: f.content.decode() for f in got.files}
    assert got.revision == edited.revision
    assert sorted(files) == [
        "case.yaml",
        "env.yaml",
        "notes/why.md",
        "prompts/dev.md",
        "prompts/qa2.md",
        "task.md",
    ]
    assert files["task.md"] == "Fix it twice." and files["notes/why.md"] == "because"
    assert {f.mode for f in got.files} == {0o644}
    old = await control.GetCaseRevision(
        pb.GetCaseRevisionRequest(workspace=workspace, case_id="demo", revision=1)
    )
    assert {f.path: f.content.decode() for f in old.files}["task.md"] == "Fix the bug."


async def test_an_edit_against_an_old_revision_is_aborted(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    await control.PushCase(pb.PushCaseRequest(case_bundle=bundle_of(tmp_path, workspace)))
    edit = pb.UpdateCaseFilesRequest(
        workspace=workspace,
        case_id="demo",
        base_revision=1,
        changes=[write_change("task.md", "mine")],
    )
    await control.UpdateCaseFiles(edit)

    edit.changes[0].content = b"theirs"
    code, details = await refused(control.UpdateCaseFiles(edit))

    assert code == grpc.StatusCode.ABORTED
    assert "is at revision 2" in details and "against revision 1" in details
    got = await control.GetCaseRevision(
        pb.GetCaseRevisionRequest(workspace=workspace, case_id="demo")
    )
    assert got.revision.revision == 2
    assert {f.path: f.content for f in got.files}["task.md"] == b"mine"


async def test_of_two_edits_at_once_one_wins_and_one_is_aborted(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    await control.PushCase(pb.PushCaseRequest(case_bundle=bundle_of(tmp_path, workspace)))

    async def edit(text: str) -> grpc.StatusCode:
        try:
            await control.UpdateCaseFiles(
                pb.UpdateCaseFilesRequest(
                    workspace=workspace,
                    case_id="demo",
                    base_revision=1,
                    changes=[write_change("task.md", text)],
                )
            )
        except grpc.aio.AioRpcError as err:
            return err.code()
        return grpc.StatusCode.OK

    codes = await asyncio.gather(*(edit(f"edit {i}") for i in range(6)))

    assert sorted(c.name for c in codes) == ["ABORTED"] * 5 + ["OK"]
    listed = await control.ListCaseRevisions(
        pb.ListCaseRevisionsRequest(workspace=workspace, case_id="demo")
    )
    assert [r.revision for r in listed.revisions] == [2, 1]


async def test_an_edit_that_changes_nothing_makes_no_revision(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    await control.PushCase(pb.PushCaseRequest(case_bundle=bundle_of(tmp_path, workspace)))

    same = await control.UpdateCaseFiles(
        pb.UpdateCaseFilesRequest(
            workspace=workspace,
            case_id="demo",
            base_revision=1,
            changes=[write_change("task.md", "Fix the bug.")],
        )
    )

    assert not same.created and same.revision.revision == 1


async def test_an_edit_with_base_zero_creates_the_case(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    case_dir = write(tmp_path, case_in(workspace))
    changes = [
        write_change(p.relative_to(case_dir).as_posix(), p.read_text())
        for p in sorted(case_dir.rglob("*"))
        if p.is_file()
    ]
    create = pb.UpdateCaseFilesRequest(
        workspace=workspace, case_id="demo", changes=changes, actor="ada"
    )

    created = await control.UpdateCaseFiles(create)
    code, details = await refused(control.UpdateCaseFiles(create))

    assert created.created and created.revision.revision == 1
    assert created.revision.bundle_sha256 == bundle_hash(pack(case_dir))
    assert code == grpc.StatusCode.ABORTED and "is at revision 1" in details


async def test_edits_the_library_cannot_take_are_refused_and_store_nothing(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    await control.PushCase(pb.PushCaseRequest(case_bundle=bundle_of(tmp_path, workspace)))

    async def edit(*changes: pb.FileChange, base: int = 1, case_id: str = "demo") -> str:
        code, details = await refused(
            control.UpdateCaseFiles(
                pb.UpdateCaseFilesRequest(
                    workspace=workspace, case_id=case_id, base_revision=base, changes=changes
                )
            )
        )
        assert code == grpc.StatusCode.INVALID_ARGUMENT, details
        return details

    renamed = yaml.safe_dump(case_in(workspace, id="other"))
    assert "no changes" in await edit()
    assert "'../escape.md'" in await edit(write_change("../escape.md", "x"))
    assert "'/etc/passwd'" in await edit(write_change("/etc/passwd", "x"))
    assert "`task.md`" in await edit(write_change("task.md/inner.md", "x"))
    assert "no such file" in await edit(pb.FileChange(path="prompts/none.md", delete=True))
    assert "more than once" in await edit(
        write_change("task.md", "a"), pb.FileChange(path="task.md", delete=True)
    )
    assert "neither writes nor deletes" in await edit(pb.FileChange(path="task.md"))
    assert "1 MiB" in await edit(write_change("big.bin", "x" * ((1 << 20) + 1)))
    assert "task.md" in await edit(pb.FileChange(path="task.md", delete=True))
    assert "names case `other`" in await edit(write_change("case.yaml", renamed))
    code, _ = await refused(
        control.UpdateCaseFiles(
            pb.UpdateCaseFilesRequest(
                workspace=workspace,
                case_id="absent",
                base_revision=3,
                changes=[write_change("task.md", "x")],
            )
        )
    )
    assert code == grpc.StatusCode.NOT_FOUND
    case = (await control.GetCase(pb.GetCaseRequest(workspace=workspace, case_id="demo"))).case
    assert case.latest.revision == 1


async def test_an_archived_case_is_hidden_and_takes_nothing_until_unarchived(
    control: "ControlServiceAsyncStub", object_store: ObjectStore, workspace: str, tmp_path: Path
) -> None:
    bundle = bundle_of(tmp_path, workspace)
    await control.PushCase(pb.PushCaseRequest(case_bundle=bundle))
    ref = pb.CaseRevisionRef(workspace=workspace, case_id="demo")

    archived = await control.ArchiveCase(
        pb.ArchiveCaseRequest(workspace=workspace, case_id="demo", actor="ada")
    )

    assert archived.case.HasField("archived_at")
    refused_bundle = bundle_of(tmp_path / "refused", workspace, {"task.md": "never stored"})
    code, _ = await refused(control.PushCase(pb.PushCaseRequest(case_bundle=refused_bundle)))
    assert code == grpc.StatusCode.FAILED_PRECONDITION
    with pytest.raises(FileNotFoundError):
        object_store.get(bundle_key(bundle_hash(refused_bundle)))
    listed = await control.ListCases(pb.ListCasesRequest(workspace=workspace))
    with_archived = await control.ListCases(
        pb.ListCasesRequest(workspace=workspace, include_archived=True)
    )
    assert [c.case_id for c in listed.cases] == []
    assert [c.case_id for c in with_archived.cases] == ["demo"]
    for call in (
        control.PushCase(
            pb.PushCaseRequest(case_bundle=bundle_of(tmp_path / "2", workspace, {"task.md": "x"}))
        ),
        control.UpdateCaseFiles(
            pb.UpdateCaseFilesRequest(
                workspace=workspace,
                case_id="demo",
                base_revision=1,
                changes=[write_change("task.md", "x")],
            )
        ),
        control.SubmitRuns(pb.SubmitRunsRequest(models=chosen(), case=ref)),
        control.SubmitRuns(pb.SubmitRunsRequest(models=chosen(), case_bundle=bundle)),
    ):
        code, details = await refused(call)
        assert code == grpc.StatusCode.FAILED_PRECONDITION and "archived" in details
    old = await control.GetCaseRevision(
        pb.GetCaseRevisionRequest(workspace=workspace, case_id="demo", revision=1)
    )
    assert old.revision.revision == 1

    restored = await control.UnarchiveCase(
        pb.UnarchiveCaseRequest(workspace=workspace, case_id="demo")
    )
    submitted = await control.SubmitRuns(pb.SubmitRunsRequest(models=chosen(), case=ref, epochs=1))

    assert not restored.case.HasField("archived_at")
    assert submitted.case_revision == 1
    for run_id in submitted.run_ids:
        await control.CancelRun(pb.CancelRunRequest(run_id=run_id))


async def test_runs_are_submitted_by_revision_and_name_it(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    first = bundle_of(tmp_path / "1", workspace)
    second = bundle_of(tmp_path / "2", workspace, {"task.md": "Fix the other bug."})
    await control.PushCase(pb.PushCaseRequest(case_bundle=first))
    await control.PushCase(pb.PushCaseRequest(case_bundle=second))
    ref = pb.CaseRevisionRef(workspace=workspace, case_id="demo")

    newest = await control.SubmitRuns(
        pb.SubmitRunsRequest(models=chosen(), case=ref, epochs=1, actor="ada")
    )
    ref.revision = 1
    pinned = await control.SubmitRuns(pb.SubmitRunsRequest(models=chosen(), case=ref, epochs=2))
    by_bundle = await control.SubmitRuns(
        pb.SubmitRunsRequest(models=chosen(), case_bundle=first, epochs=1)
    )

    assert (newest.case_revision, pinned.case_revision, by_bundle.case_revision) == (2, 1, 3)
    assert len(pinned.run_ids) == 2
    # The workspace filter is the server's: other tests' `demo` runs in other workspaces are
    # not among the newest few it would otherwise return.
    listed = await control.ListRuns(pb.ListRunsRequest(workspace=workspace, limit=4))
    assert {r.workspace for r in listed.runs} == {workspace} and len(listed.runs) == 4
    elsewhere = await control.ListRuns(pb.ListRunsRequest(workspace=f"{workspace}-x"))
    assert list(elsewhere.runs) == []
    runs = {r.run_id: r for r in listed.runs}
    assert runs[newest.run_ids[0]].case_revision == 2
    assert runs[newest.run_ids[0]].case_sha256 == bundle_hash(second)
    assert runs[newest.run_ids[0]].submitted_by == "ada"
    assert {runs[r].case_revision for r in pinned.run_ids} == {1}
    assert runs[pinned.run_ids[0]].case_sha256 == bundle_hash(first)
    for run_id in runs:
        await control.CancelRun(pb.CancelRunRequest(run_id=run_id))

    ref.revision = 9
    code, details = await refused(
        control.SubmitRuns(pb.SubmitRunsRequest(models=chosen(), case=ref))
    )
    assert code == grpc.StatusCode.NOT_FOUND and "no revision 9" in details
    code, _ = await refused(
        control.SubmitRuns(pb.SubmitRunsRequest(models=chosen(), case=ref, case_bundle=first))
    )
    assert code == grpc.StatusCode.INVALID_ARGUMENT
    code, _ = await refused(control.SubmitRuns(pb.SubmitRunsRequest()))
    assert code == grpc.StatusCode.INVALID_ARGUMENT


async def test_a_refused_submission_leaves_no_revision(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    bundle = bundle_of(tmp_path, workspace)

    code, _ = await refused(
        control.SubmitRuns(
            pb.SubmitRunsRequest(models=chosen(), case_bundle=bundle, suite="Not A Label")
        )
    )

    assert code == grpc.StatusCode.INVALID_ARGUMENT
    listed = await control.ListCases(pb.ListCasesRequest(workspace=workspace))
    assert list(listed.cases) == []
