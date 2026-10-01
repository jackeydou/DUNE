"""The run queue: rows in `control.runs` and `control.run_specs` (docs/services/orchestrator.md).

The control plane only inserts and reads; a worker claims with `FOR UPDATE SKIP LOCKED`, which
sets the owner and increments `owner_epoch` in the same statement. Shared by both roles.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import JsonValue
from sqlalchemy import Row, Select, case, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.db import control_runs, run_specs

RunStatus = Literal["queued", "running", "paused", "interrupted", "done", "failed", "cancelled"]
FINISHED: tuple[RunStatus, ...] = ("interrupted", "done", "failed", "cancelled")


class RunNotFound(Exception):
    pass


class RunFinished(Exception):
    """The run has already finished, so the request no longer applies."""


@dataclass(frozen=True)
class NewRun:
    run_id: str
    submission_id: str
    case_id: str
    workspace: str
    case_sha256: str
    overrides: dict[str, JsonValue]
    variant: int
    task_args: dict[str, JsonValue]
    epoch: int
    epochs: int


@dataclass(frozen=True)
class RunRow:
    run_id: str
    submission_id: str
    case_id: str
    workspace: str
    status: RunStatus
    case_sha256: str
    overrides: dict[str, JsonValue]
    variant: int
    task_args: dict[str, JsonValue]
    epoch: int
    epochs: int
    owner_id: str | None
    owner_epoch: int
    isolation: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _joined() -> Select[*tuple[Any, ...]]:
    r, s = control_runs.c, run_specs.c
    return select(
        r.run_id,
        s.submission_id,
        s.case_id,
        r.workspace,
        r.status,
        s.case_sha256,
        s.overrides,
        s.variant,
        s.task_args,
        s.epoch,
        s.epochs,
        r.owner_id,
        r.owner_epoch,
        r.isolation,
        r.error,
        r.created_at,
        r.started_at,
        r.finished_at,
    ).join_from(control_runs, run_specs, r.run_id == s.run_id)


def _row(row: Row[*tuple[Any, ...]]) -> RunRow:
    # `_asdict` is public API; the underscore only avoids clashing with column names.
    return RunRow(**row._asdict())  # pyright: ignore[reportPrivateUsage]


class Queue:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def enqueue(self, runs: Sequence[NewRun]) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(control_runs),
                [{"run_id": r.run_id, "workspace": r.workspace, "status": "queued"} for r in runs],
            )
            await conn.execute(
                insert(run_specs),
                [
                    {
                        "run_id": r.run_id,
                        "submission_id": r.submission_id,
                        "case_id": r.case_id,
                        "case_sha256": r.case_sha256,
                        "overrides": r.overrides,
                        "variant": r.variant,
                        "task_args": r.task_args,
                        "epoch": r.epoch,
                        "epochs": r.epochs,
                    }
                    for r in runs
                ],
            )

    async def get(self, run_id: str) -> RunRow:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(_joined().where(control_runs.c.run_id == run_id))
            ).one_or_none()
        if row is None:
            raise RunNotFound(f"no run `{run_id}`.")
        return _row(row)

    async def list_runs(
        self,
        *,
        submission_id: str | None = None,
        case_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[RunRow]:
        query = _joined().order_by(control_runs.c.created_at.desc(), control_runs.c.run_id)
        if submission_id:
            query = query.where(run_specs.c.submission_id == submission_id)
        if case_id:
            query = query.where(run_specs.c.case_id == case_id)
        if status:
            query = query.where(control_runs.c.status == status)
        async with self._engine.connect() as conn:
            rows = await conn.execute(query.limit(limit))
            return [_row(r) for r in rows]

    async def cancel(self, run_id: str) -> RunRow:
        """A queued run finishes now; a running one when its worker sees the status."""
        runs = control_runs.c
        async with self._engine.begin() as conn:
            updated = await conn.execute(
                update(control_runs)
                .where(runs.run_id == run_id, runs.status.in_(("queued", "running", "paused")))
                .values(
                    status="cancelled",
                    finished_at=case((runs.owner_id.is_(None), func.now()), else_=None),
                )
                .returning(runs.run_id)
            )
            cancelled = updated.scalar_one_or_none()
        if cancelled is None:
            current = await self.get(run_id)
            raise RunFinished(
                f"run `{run_id}` is already `{current.status}`; only queued, running, or paused "
                "runs can be cancelled."
            )
        return await self.get(run_id)

    async def claim(self, owner_id: str) -> RunRow | None:
        """The oldest queued run, now owned by `owner_id` at a new `owner_epoch`."""
        runs = control_runs.c
        oldest = (
            select(runs.run_id)
            .where(runs.status == "queued")
            .order_by(runs.created_at, runs.run_id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        async with self._engine.begin() as conn:
            claimed = (
                await conn.execute(
                    update(control_runs)
                    .where(runs.run_id == oldest)
                    .values(
                        status="running",
                        owner_id=owner_id,
                        owner_epoch=runs.owner_epoch + 1,
                        started_at=func.now(),
                    )
                    .returning(runs.run_id)
                )
            ).scalar_one_or_none()
        return None if claimed is None else await self.get(claimed)

    async def status(self, run_id: str) -> RunStatus:
        async with self._engine.connect() as conn:
            found = (
                await conn.execute(
                    select(control_runs.c.status).where(control_runs.c.run_id == run_id)
                )
            ).scalar_one_or_none()
        if found is None:
            raise RunNotFound(f"no run `{run_id}`.")
        return found

    async def set_isolation(self, run_id: str, owner_epoch: int, isolation: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(control_runs)
                .where(control_runs.c.run_id == run_id, control_runs.c.owner_epoch == owner_epoch)
                .values(isolation=isolation)
            )

    async def finish(
        self, run_id: str, owner_epoch: int, status: RunStatus, error: str | None = None
    ) -> None:
        """Records the outcome. A run cancelled meanwhile stays `cancelled`."""
        runs = control_runs.c
        async with self._engine.begin() as conn:
            await conn.execute(
                update(control_runs)
                .where(runs.run_id == run_id, runs.owner_epoch == owner_epoch)
                .values(
                    status=case((runs.status == "cancelled", "cancelled"), else_=status),
                    error=error,
                    finished_at=func.now(),
                )
            )

    async def interrupt_owned(self, owner_id: str) -> list[str]:
        """Marks runs a previous process of this worker left running as `interrupted`. M0 has no
        takeover; M2 resumes them instead."""
        runs = control_runs.c
        async with self._engine.begin() as conn:
            rows = await conn.execute(
                update(control_runs)
                .where(runs.owner_id == owner_id, runs.status.in_(("running", "paused")))
                .values(
                    status="interrupted",
                    error="the worker that owned the run restarted",
                    finished_at=func.now(),
                )
                .returning(runs.run_id)
            )
            return list(rows.scalars())
