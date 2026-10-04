"""The Control API's case library RPCs (docs/services/orchestrator.md#case-library), and the
checks every case passes before it is stored or run."""

import asyncio
import logging
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.bundles import (
    MAX_BUNDLE_BYTES,
    apply_changes,
    bundle_hash,
    bundle_key,
    files_of,
    unpack,
)
from swarmeval.control.cases import (
    CaseArchived,
    CaseLibrary,
    CaseNotFound,
    CaseRow,
    RevisionConflict,
    RevisionNotFound,
    RevisionRow,
    add_revision,
    conflict,
    refuse_archived,
)
from swarmeval.core import CaseError, LoadedCase, load_case
from swarmeval.core.models import AxisValue
from swarmeval.events import ObjectStore
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb

Context = grpc.aio.ServicerContext[Any, Any]

MAX_EDIT_FILE_BYTES = 1 << 20
"""The largest file `UpdateCaseFiles` writes. Larger files arrive with a pushed directory."""

MAX_NOTE_CHARS = 1_000

_log = logging.getLogger(__name__)


def load_bundle(bundle: bytes, overrides: Mapping[str, Sequence[AxisValue]]) -> LoadedCase:
    with tempfile.TemporaryDirectory(prefix="swarmeval-case-") as scratch:
        return load_case(unpack(bundle, Path(scratch)), overrides)


def timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value)
    return stamp


def revision_proto(row: RevisionRow) -> pb.CaseRevision:
    return pb.CaseRevision(
        workspace=row.workspace,
        case_id=row.case_id,
        revision=row.revision,
        bundle_sha256=row.bundle_sha256,
        actor=row.actor or "",
        note=row.note or "",
        created_at=timestamp(row.created_at),
    )


def case_proto(row: CaseRow) -> pb.Case:
    return pb.Case(
        workspace=row.workspace,
        case_id=row.case_id,
        created_at=timestamp(row.created_at),
        archived_at=timestamp(row.archived_at),
        latest=revision_proto(row.latest),
    )


class CaseRpcs:
    """The case RPCs of `ControlService`, which provides the attributes."""

    _engine: AsyncEngine
    _store: ObjectStore
    _library: CaseLibrary
    _allow_case_code: bool

    async def _accept(
        self, bundle: bytes, overrides: Mapping[str, Sequence[AxisValue]], context: Context
    ) -> LoadedCase:
        """The case in `bundle`, loaded as a worker will load it. Aborts for a case that does
        not load, and for one with case code where the deployment runs none."""
        try:
            loaded = await asyncio.to_thread(load_bundle, bundle, overrides)
        except CaseError as err:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(err))
        await self._refuse_case_code(loaded, context)
        return loaded

    async def _refuse_case_code(self, loaded: LoadedCase, context: Context) -> None:
        code = sorted({use for v in loaded.variants for use in v.code})
        if code and not self._allow_case_code:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"case `{loaded.id}` loads extensions from its own directory "
                f"({', '.join(code)}), and this deployment does not run case code: it would run "
                "inside the workers with their privileges. Start the control plane and the "
                "workers with `--allow-case-code` to accept it.",
            )

    async def _note(self, note: str, context: Context) -> str | None:
        if len(note) > MAX_NOTE_CHARS:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"the note is {len(note)} characters; the limit is {MAX_NOTE_CHARS}.",
            )
        return note or None

    async def _stored_revision(
        self, workspace: str, case_id: str, revision: int, context: Context
    ) -> RevisionRow:
        try:
            return await self._library.revision(workspace, case_id, revision)
        except (CaseNotFound, RevisionNotFound) as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))

    async def PushCase(self, request: pb.PushCaseRequest, context: Context) -> pb.PushCaseResponse:
        note = await self._note(request.note, context)
        loaded = await self._accept(request.case_bundle, {}, context)
        sha256 = bundle_hash(request.case_bundle)
        await asyncio.to_thread(self._store.put, bundle_key(sha256), request.case_bundle)
        try:
            async with self._engine.begin() as conn:
                revision, created = await add_revision(
                    conn,
                    workspace=loaded.workspace,
                    case_id=loaded.id,
                    sha256=sha256,
                    actor=request.actor or None,
                    note=note,
                )
        except CaseArchived as err:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(err))
        return pb.PushCaseResponse(revision=revision_proto(revision), created=created)

    async def UpdateCaseFiles(
        self, request: pb.UpdateCaseFilesRequest, context: Context
    ) -> pb.UpdateCaseFilesResponse:
        workspace, case_id, base_revision = (
            request.workspace,
            request.case_id,
            request.base_revision,
        )
        note = await self._note(request.note, context)
        writes, deletes = await self._changes(request, context)
        try:
            base_row = await self._library.revision(workspace, case_id)
        except CaseNotFound as err:
            if base_revision:
                await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
            base_row = None
        base = None
        if base_row is not None:
            try:
                refuse_archived(base_row)
            except CaseArchived as err:
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(err))
            if base_row.revision != base_revision:
                # Checked again under the case's lock when the revision is added; this one
                # spares a stale editor the wait for validation.
                await context.abort(
                    grpc.StatusCode.ABORTED,
                    str(conflict(workspace, case_id, base_row.revision, base_revision)),
                )
            base = await asyncio.to_thread(self._store.get, bundle_key(base_row.bundle_sha256))
        try:
            bundle, changed = await asyncio.to_thread(apply_changes, base, writes, deletes)
        except CaseError as err:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(err))
        if base_row is not None and not changed:
            return pb.UpdateCaseFilesResponse(revision=revision_proto(base_row), created=False)
        if len(bundle) > MAX_BUNDLE_BYTES:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"case `{case_id}` would be {len(bundle)} bytes packed; the limit is "
                f"{MAX_BUNDLE_BYTES} (64 MiB). Move large data out of the case.",
            )
        loaded = await self._accept(bundle, {}, context)
        if (loaded.workspace, loaded.id) != (workspace, case_id):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"`case.yaml` now names case `{loaded.id}` in workspace `{loaded.workspace}`, "
                f"and the edit is to case `{case_id}` in workspace `{workspace}`. A case's "
                "`id` and `workspace` are its identity: to rename it, save the files as a new "
                "case and archive this one.",
            )
        sha256 = bundle_hash(bundle)
        await asyncio.to_thread(self._store.put, bundle_key(sha256), bundle)
        try:
            async with self._engine.begin() as conn:
                revision, created = await add_revision(
                    conn,
                    workspace=workspace,
                    case_id=case_id,
                    sha256=sha256,
                    actor=request.actor or None,
                    note=note,
                    base_revision=base_revision,
                )
        except RevisionConflict as err:
            await context.abort(grpc.StatusCode.ABORTED, str(err))
        except CaseArchived as err:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(err))
        return pb.UpdateCaseFilesResponse(revision=revision_proto(revision), created=created)

    async def _changes(
        self, request: pb.UpdateCaseFilesRequest, context: Context
    ) -> tuple[dict[str, bytes], set[str]]:
        writes: dict[str, bytes] = {}
        deletes: set[str] = set()
        problem = "" if request.changes else "the request lists no changes."
        for change in request.changes:
            kind = change.WhichOneof("change")
            if change.path in writes or change.path in deletes:
                problem = f"`{change.path}` is changed more than once. Send each path once."
            elif kind == "content" and len(change.content) > MAX_EDIT_FILE_BYTES:
                problem = (
                    f"`{change.path}` is {len(change.content)} bytes; an edit writes files of "
                    f"at most {MAX_EDIT_FILE_BYTES} (1 MiB). Push the case directory to store "
                    "a larger file."
                )
            elif kind == "content":
                writes[change.path] = change.content
            elif kind == "delete" and change.delete:
                deletes.add(change.path)
            else:
                problem = (
                    f"the change to `{change.path}` neither writes nor deletes. Set `content`, "
                    "or `delete: true`."
                )
            if problem:
                break
        if problem:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"case edit: {problem}")
        return writes, deletes

    async def GetCase(self, request: pb.GetCaseRequest, context: Context) -> pb.GetCaseResponse:
        try:
            found = await self._library.get(request.workspace, request.case_id)
        except CaseNotFound as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
        return pb.GetCaseResponse(case=case_proto(found))

    async def ListCases(
        self, request: pb.ListCasesRequest, context: Context
    ) -> pb.ListCasesResponse:
        found = await self._library.list_cases(
            workspace=request.workspace or None, include_archived=request.include_archived
        )
        return pb.ListCasesResponse(cases=[case_proto(c) for c in found])

    async def ListCaseRevisions(
        self, request: pb.ListCaseRevisionsRequest, context: Context
    ) -> pb.ListCaseRevisionsResponse:
        try:
            found = await self._library.revisions(request.workspace, request.case_id)
        except CaseNotFound as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
        return pb.ListCaseRevisionsResponse(revisions=[revision_proto(r) for r in found])

    async def GetCaseRevision(
        self, request: pb.GetCaseRevisionRequest, context: Context
    ) -> pb.GetCaseRevisionResponse:
        revision = await self._stored_revision(
            request.workspace, request.case_id, request.revision, context
        )
        bundle = await asyncio.to_thread(self._store.get, bundle_key(revision.bundle_sha256))
        files = await asyncio.to_thread(files_of, bundle)
        return pb.GetCaseRevisionResponse(
            revision=revision_proto(revision),
            files=[
                pb.CaseFile(path=f.path, content=f.content, mode=f.mode, link_target=f.link_target)
                for f in files
            ],
        )

    async def ArchiveCase(
        self, request: pb.ArchiveCaseRequest, context: Context
    ) -> pb.ArchiveCaseResponse:
        return pb.ArchiveCaseResponse(
            case=await self._set_archived(request, archived=True, context=context)
        )

    async def UnarchiveCase(
        self, request: pb.UnarchiveCaseRequest, context: Context
    ) -> pb.UnarchiveCaseResponse:
        return pb.UnarchiveCaseResponse(
            case=await self._set_archived(request, archived=False, context=context)
        )

    async def _set_archived(
        self,
        request: pb.ArchiveCaseRequest | pb.UnarchiveCaseRequest,
        *,
        archived: bool,
        context: Context,
    ) -> pb.Case:
        try:
            found = await self._library.set_archived(request.workspace, request.case_id, archived)
        except CaseNotFound as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
        # The tables record who made each revision; who archived a case is only logged.
        _log.info(
            "case %s/%s %s by %s",
            request.workspace,
            request.case_id,
            "archived" if archived else "unarchived",
            request.actor or "(no actor)",
        )
        return case_proto(found)
