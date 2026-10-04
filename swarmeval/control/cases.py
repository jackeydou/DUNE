"""The case library: rows in `control.cases` and `control.case_revisions`
(docs/services/orchestrator.md#case-library).

A case is a `case.yaml`'s `workspace` and `id`. Its revisions are numbered from 1, each naming a
bundle by hash, and are never changed or deleted: every run references one. Writers lock the
case's row, so two of them take different numbers.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Row, Select, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from swarmeval.db import case_revisions, cases


class CaseNotFound(Exception):
    pass


class RevisionNotFound(Exception):
    pass


class CaseArchived(Exception):
    """An archived case takes no pushes, edits, or runs."""


class RevisionConflict(Exception):
    """The revision an edit was made against is no longer the case's newest."""


@dataclass(frozen=True)
class RevisionRow:
    id: int
    workspace: str
    case_id: str
    revision: int
    bundle_sha256: str
    actor: str | None
    note: str | None
    created_at: datetime
    archived_at: datetime | None
    """The case's, not the revision's: revisions are never archived on their own."""


@dataclass(frozen=True)
class CaseRow:
    workspace: str
    case_id: str
    created_at: datetime
    archived_at: datetime | None
    latest: RevisionRow


def _revisions() -> Select[*tuple[Any, ...]]:
    v, c = case_revisions.c, cases.c
    return select(
        v.id,
        c.workspace,
        c.case_id,
        v.revision,
        v.bundle_sha256,
        v.actor,
        v.note,
        v.created_at,
        c.archived_at,
    ).join_from(case_revisions, cases, v.case_pk == c.id)


def _newest() -> Select[*tuple[Any, ...]]:
    """Each case's newest revision, with the case's own `created_at` last."""
    others = case_revisions.alias()
    newest = (
        select(func.max(others.c.revision)).where(others.c.case_pk == cases.c.id).scalar_subquery()
    )
    return (
        _revisions()
        .add_columns(cases.c.created_at.label("case_created_at"))
        .where(case_revisions.c.revision == newest)
    )


def _revision(row: Row[*tuple[Any, ...]]) -> RevisionRow:
    return RevisionRow(*row[:9])


def _case(row: Row[*tuple[Any, ...]]) -> CaseRow:
    latest = _revision(row)
    return CaseRow(latest.workspace, latest.case_id, row[9], latest.archived_at, latest)


def _no_case(workspace: str, case_id: str) -> CaseNotFound:
    return CaseNotFound(
        f"no case `{case_id}` in workspace `{workspace}`. List cases to see what the library "
        "holds, or push the case directory first."
    )


def _archived(workspace: str, case_id: str) -> CaseArchived:
    return CaseArchived(
        f"case `{case_id}` in workspace `{workspace}` is archived, and takes no pushes, edits, "
        "or runs. Unarchive it first."
    )


def conflict(workspace: str, case_id: str, newest: int, base: int) -> RevisionConflict:
    """`newest` is 0 when the case has no revision."""
    return RevisionConflict(
        f"case `{case_id}` in workspace `{workspace}` "
        + (f"is at revision {newest}" if newest else "does not exist")
        + f", and the changes were made against revision {base}. Load "
        + (f"revision {newest}" if newest else "the case list")
        + " and apply them again."
    )


async def add_revision(
    conn: AsyncConnection,
    *,
    workspace: str,
    case_id: str,
    sha256: str,
    actor: str | None,
    note: str | None,
    base_revision: int | None = None,
) -> tuple[RevisionRow, bool]:
    """Makes bundle `sha256` the case's newest revision in `conn`'s transaction, creating the
    case if it is new, and returns the revision and whether it was added: a bundle that is the
    newest revision already adds none.

    With `base_revision`, the newest revision must be that one (0: the case has none yet), or
    `RevisionConflict` is raised. Raises `CaseArchived` for an archived case. The caller rolls
    the transaction back on either."""
    await conn.execute(
        insert(cases)
        .values(workspace=workspace, case_id=case_id)
        .on_conflict_do_nothing(index_elements=["workspace", "case_id"])
    )
    # The lock orders the writers of one case: the second sees the first's revision.
    case_pk, archived_at = (
        await conn.execute(
            select(cases.c.id, cases.c.archived_at)
            .where(cases.c.workspace == workspace, cases.c.case_id == case_id)
            .with_for_update()
        )
    ).one()
    if archived_at is not None:
        raise _archived(workspace, case_id)
    found = (
        await conn.execute(
            _revisions()
            .where(case_revisions.c.case_pk == case_pk)
            .order_by(case_revisions.c.revision.desc())
            .limit(1)
        )
    ).one_or_none()
    latest = None if found is None else _revision(found)
    newest = 0 if latest is None else latest.revision
    if base_revision is not None and base_revision != newest:
        raise conflict(workspace, case_id, newest, base_revision)
    if latest is not None and latest.bundle_sha256 == sha256:
        return latest, False
    revision_id = (
        await conn.execute(
            insert(case_revisions)
            .values(
                case_pk=case_pk,
                revision=newest + 1,
                bundle_sha256=sha256,
                actor=actor,
                note=note,
            )
            .returning(case_revisions.c.id)
        )
    ).scalar_one()
    added = await conn.execute(_revisions().where(case_revisions.c.id == revision_id))
    return _revision(added.one()), True


class CaseLibrary:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, workspace: str, case_id: str) -> CaseRow:
        query = _newest().where(cases.c.workspace == workspace, cases.c.case_id == case_id)
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).one_or_none()
        if row is None:
            raise _no_case(workspace, case_id)
        return _case(row)

    async def list_cases(
        self, *, workspace: str | None = None, include_archived: bool = False
    ) -> list[CaseRow]:
        query = _newest().order_by(cases.c.workspace, cases.c.case_id)
        if workspace:
            query = query.where(cases.c.workspace == workspace)
        if not include_archived:
            query = query.where(cases.c.archived_at.is_(None))
        async with self._engine.connect() as conn:
            return [_case(row) for row in await conn.execute(query)]

    async def revisions(self, workspace: str, case_id: str) -> list[RevisionRow]:
        """Newest first."""
        query = (
            _revisions()
            .where(cases.c.workspace == workspace, cases.c.case_id == case_id)
            .order_by(case_revisions.c.revision.desc())
        )
        async with self._engine.connect() as conn:
            found = [_revision(row) for row in await conn.execute(query)]
        if not found:
            raise _no_case(workspace, case_id)
        return found

    async def revision(self, workspace: str, case_id: str, revision: int = 0) -> RevisionRow:
        """Revision `revision` of the case; the newest for 0."""
        latest = (await self.get(workspace, case_id)).latest
        if revision in (0, latest.revision):
            return latest
        query = _revisions().where(
            cases.c.workspace == workspace,
            cases.c.case_id == case_id,
            case_revisions.c.revision == revision,
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).one_or_none()
        if row is None:
            raise RevisionNotFound(
                f"case `{case_id}` in workspace `{workspace}` has no revision {revision}; its "
                f"newest is {latest.revision}."
            )
        return _revision(row)

    async def set_archived(self, workspace: str, case_id: str, archived: bool) -> CaseRow:
        """Archives the case, or takes it back. Archiving an archived case keeps its time."""
        async with self._engine.begin() as conn:
            await conn.execute(
                update(cases)
                .where(
                    cases.c.workspace == workspace,
                    cases.c.case_id == case_id,
                    cases.c.archived_at.is_(None) if archived else cases.c.archived_at.is_not(None),
                )
                .values(archived_at=func.now() if archived else None)
            )
        return await self.get(workspace, case_id)


def refuse_archived(revision: RevisionRow) -> None:
    if revision.archived_at is not None:
        raise _archived(revision.workspace, revision.case_id)
