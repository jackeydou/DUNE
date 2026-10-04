"""The run queue: rows in `control.runs` and `control.run_specs` (docs/services/orchestrator.md).

The control plane only inserts and reads; a worker claims with `FOR UPDATE SKIP LOCKED`, which
sets the owner and increments `owner_epoch` in the same statement. Shared by both roles.

A claim also gives the run a lease, which its owner renews; a run whose lease ran out can be
claimed by another worker (docs/services/orchestrator.md#leases-fencing-and-takeover). All lease
times are the database's clock, so workers' clocks never have to agree.

A run that becomes `interrupted` gets its rerun in the same transaction: one queued run for the
same submission and variant at the next unused epoch, up to `epochs` reruns per variant
(docs/services/orchestrator.md#reruns).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import JsonValue
from sqlalchemy import ColumnElement, Row, Select, case, func, insert, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from swarmeval.db import case_revisions, control_runs, run_specs
from swarmeval.events.store import FencedError

RunStatus = Literal["queued", "running", "paused", "interrupted", "done", "failed", "cancelled"]
FINISHED: tuple[RunStatus, ...] = ("interrupted", "done", "failed", "cancelled")
_FINISHABLE: tuple[RunStatus, ...] = ("running", "paused", "cancelled")
"""What `finish` may change. `cancelled` because a cancel while the run ran waits for its owner."""

LEASE_S = 30.0
"""Default lease length in seconds. The owner renews every third of it."""


def _lease_end(lease_s: float) -> ColumnElement[datetime]:
    return func.now() + timedelta(seconds=lease_s)


class RunNotFound(Exception):
    pass


class RunFinished(Exception):
    """The run has already finished, so the request no longer applies."""


class RunNotPaused(Exception):
    """Only a paused run can be resumed."""


def run_id_of(case_id: str, submission_id: str, variant: int, epoch: int) -> str:
    return f"{case_id}.{submission_id}.v{variant}.e{epoch}"


@dataclass(frozen=True)
class NewRun:
    run_id: str
    submission_id: str
    case_id: str
    workspace: str
    case_sha256: str
    case_revision_id: int
    """The `control.case_revisions` row whose bundle `case_sha256` is."""
    overrides: dict[str, JsonValue]
    variant: int
    task_args: dict[str, JsonValue]
    epoch: int
    epochs: int
    replaces: str | None = None
    suite: str | None = None
    submitted_by: str | None = None


@dataclass(frozen=True)
class RunRow:
    run_id: str
    submission_id: str
    case_id: str
    workspace: str
    status: RunStatus
    case_sha256: str
    case_revision_id: int
    case_revision: int
    """The revision's number within its case."""
    overrides: dict[str, JsonValue]
    variant: int
    task_args: dict[str, JsonValue]
    epoch: int
    epochs: int
    replaces: str | None
    suite: str | None
    forked_from: str | None
    fork_seq: int | None
    fork_edits: list[JsonValue] | None
    submitted_by: str | None
    cancelled_by: str | None
    resumed_by: str | None
    owner_id: str | None
    owner_epoch: int
    isolation: str | None
    fidelity: str | None
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
        s.case_revision_id,
        case_revisions.c.revision.label("case_revision"),
        s.overrides,
        s.variant,
        s.task_args,
        s.epoch,
        s.epochs,
        s.replaces,
        s.suite,
        s.forked_from,
        s.fork_seq,
        s.fork_edits,
        s.submitted_by,
        r.cancelled_by,
        r.resumed_by,
        r.owner_id,
        r.owner_epoch,
        r.isolation,
        r.fidelity,
        r.error,
        r.created_at,
        r.started_at,
        r.finished_at,
    ).select_from(
        control_runs.join(run_specs, r.run_id == s.run_id).join(
            case_revisions, s.case_revision_id == case_revisions.c.id
        )
    )


def _row(row: Row[*tuple[Any, ...]]) -> RunRow:
    # `_asdict` is public API; the underscore only avoids clashing with column names.
    return RunRow(**row._asdict())  # pyright: ignore[reportPrivateUsage]


@dataclass(frozen=True)
class Recovered:
    """A run a restarted worker found it still owned, now finished."""

    run_id: str
    status: RunStatus
    """`interrupted`, or `cancelled` for a run cancelled while it ran."""
    replacement: str | None
    """The rerun queued for an interrupted run; `None` once its variant used up its reruns."""


@dataclass(frozen=True)
class Expired:
    """A run whose owner stopped renewing its lease, now claimed by another worker."""

    run: RunRow
    """As claimed: the new owner and `owner_epoch`, and the status the run had (`running`,
    `paused`, or `cancelled` while it ran)."""
    previous_owner: str
    expired_at: datetime


async def insert_runs(conn: AsyncConnection, runs: Sequence[NewRun]) -> None:
    """Queues `runs` in `conn`'s transaction, so they are queued together with whatever else it
    writes, or not at all."""
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
                "case_revision_id": r.case_revision_id,
                "overrides": r.overrides,
                "variant": r.variant,
                "task_args": r.task_args,
                "epoch": r.epoch,
                "epochs": r.epochs,
                "replaces": r.replaces,
                "suite": r.suite,
                "submitted_by": r.submitted_by,
            }
            for r in runs
        ],
    )


class Queue:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def enqueue(self, runs: Sequence[NewRun]) -> None:
        async with self._engine.begin() as conn:
            await insert_runs(conn, runs)

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
        suite: str | None = None,
        limit: int = 100,
    ) -> list[RunRow]:
        query = _joined().order_by(control_runs.c.created_at.desc(), control_runs.c.run_id)
        if submission_id:
            query = query.where(run_specs.c.submission_id == submission_id)
        if case_id:
            query = query.where(run_specs.c.case_id == case_id)
        if status:
            query = query.where(control_runs.c.status == status)
        if suite:
            query = query.where(run_specs.c.suite == suite)
        async with self._engine.connect() as conn:
            rows = await conn.execute(query.limit(limit))
            return [_row(r) for r in rows]

    async def cancel(self, run_id: str, actor: str | None = None) -> RunRow:
        """A queued run finishes now; a running one when its worker sees the status."""
        runs = control_runs.c
        async with self._engine.begin() as conn:
            updated = await conn.execute(
                update(control_runs)
                .where(runs.run_id == run_id, runs.status.in_(("queued", "running", "paused")))
                .values(
                    status="cancelled",
                    finished_at=case((runs.owner_id.is_(None), func.now()), else_=None),
                    cancelled_by=actor,
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

    async def pause(self, run_id: str, owner_epoch: int) -> RunStatus:
        """A running run becomes `paused`, until `resume`. Returns the status after: `paused`,
        or whatever it already was (`cancelled` by a cancel that came first)."""
        runs = control_runs.c
        async with self._engine.begin() as conn:
            paused = (
                await conn.execute(
                    update(control_runs)
                    .where(
                        runs.run_id == run_id,
                        runs.owner_epoch == owner_epoch,
                        runs.status == "running",
                    )
                    .values(status="paused")
                    .returning(runs.status)
                )
            ).scalar_one_or_none()
        return paused or await self.status(run_id)

    async def resume(self, run_id: str, actor: str | None = None) -> RunRow:
        """A paused run is `running` again; its worker sees it and goes on."""
        runs = control_runs.c
        async with self._engine.begin() as conn:
            resumed = (
                await conn.execute(
                    update(control_runs)
                    .where(runs.run_id == run_id, runs.status == "paused")
                    .values(status="running", resumed_by=actor)
                    .returning(runs.run_id)
                )
            ).scalar_one_or_none()
        if resumed is None:
            current = await self.get(run_id)
            raise RunNotPaused(
                f"run `{run_id}` is `{current.status}`, not paused; only a paused run can be "
                "resumed."
            )
        return await self.get(run_id)

    async def claim(self, owner_id: str, lease_s: float = LEASE_S) -> RunRow | None:
        """The oldest queued run, now owned by `owner_id` at a new `owner_epoch`, with a lease
        of `lease_s` from now."""
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
                        lease_until=_lease_end(lease_s),
                        started_at=func.now(),
                    )
                    .returning(runs.run_id)
                )
            ).scalar_one_or_none()
        return None if claimed is None else await self.get(claimed)

    async def claim_expired(self, owner_id: str, lease_s: float = LEASE_S) -> Expired | None:
        """The unfinished run whose lease ran out first, now owned by `owner_id` at a new
        `owner_epoch` with a lease of `lease_s` from now. Its status is left as it was. The
        epoch bump fences the previous owner: its next write raises `FencedError`, and its
        `finish` does nothing.

        A run claimed before leases existed has none, never expires, and is still finished by
        `interrupt_owned` when its worker restarts."""
        runs = control_runs.c
        stale = (
            select(
                runs.run_id,
                runs.owner_id.label("previous_owner"),
                runs.lease_until.label("expired_at"),
            )
            .where(
                runs.status.in_(_FINISHABLE),
                runs.finished_at.is_(None),
                runs.lease_until < func.now(),
            )
            .order_by(runs.lease_until, runs.run_id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .subquery()
        )
        async with self._engine.begin() as conn:
            claimed = (
                await conn.execute(
                    update(control_runs)
                    .where(runs.run_id == stale.c.run_id)
                    .values(
                        owner_id=owner_id,
                        owner_epoch=runs.owner_epoch + 1,
                        lease_until=_lease_end(lease_s),
                    )
                    .returning(runs.run_id, stale.c.previous_owner, stale.c.expired_at)
                )
            ).one_or_none()
        if claimed is None:
            return None
        run_id, previous_owner, expired_at = claimed
        return Expired(await self.get(run_id), previous_owner, expired_at)

    async def renew(
        self, owner_id: str, held: Mapping[str, int], lease_s: float = LEASE_S
    ) -> set[str]:
        """Extends to `lease_s` from now the lease of each unfinished run `owner_id` holds at the
        given `owner_epoch` (`run_id` → epoch). Returns the runs renewed; one left out was taken
        over or finished elsewhere, and its owner must stop executing it."""
        if not held:
            return set()
        runs = control_runs.c
        async with self._engine.begin() as conn:
            renewed = await conn.execute(
                update(control_runs)
                .where(
                    runs.owner_id == owner_id,
                    tuple_(runs.run_id, runs.owner_epoch).in_(list(held.items())),
                    runs.finished_at.is_(None),
                )
                .values(lease_until=_lease_end(lease_s))
                .returning(runs.run_id)
            )
            return set(renewed.scalars())

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

    async def fork(
        self,
        source: RunRow,
        fork_seq: int,
        edits: list[JsonValue],
        actor: str | None = None,
    ) -> RunRow:
        """Queues a fork of `source`, which goes on after its event `fork_seq` with `edits`:
        the same case revision, variant, and epoch (so the same seed), as run
        `<source>.f<n>`. Its reports keep it apart from the epochs."""
        async with self._engine.begin() as conn:
            # Serializes the forks of one run, so two take different numbers.
            lock = f"swarmeval.fork:{source.run_id}"
            await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock, 0))))
            forks = (
                await conn.execute(
                    select(func.count()).where(run_specs.c.forked_from == source.run_id)
                )
            ).scalar_one()
            run_id = f"{source.run_id}.f{forks + 1}"
            await conn.execute(
                insert(control_runs).values(
                    run_id=run_id, workspace=source.workspace, status="queued"
                )
            )
            await conn.execute(
                insert(run_specs).values(
                    run_id=run_id,
                    submission_id=source.submission_id,
                    case_id=source.case_id,
                    case_sha256=source.case_sha256,
                    case_revision_id=source.case_revision_id,
                    overrides=source.overrides,
                    variant=source.variant,
                    task_args=source.task_args,
                    epoch=source.epoch,
                    epochs=source.epochs,
                    suite=source.suite,
                    forked_from=source.run_id,
                    fork_seq=fork_seq,
                    fork_edits=edits,
                    submitted_by=actor,
                )
            )
        return await self.get(run_id)

    async def set_fidelity(self, run_id: str, owner_epoch: int, fidelity: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(control_runs)
                .where(control_runs.c.run_id == run_id, control_runs.c.owner_epoch == owner_epoch)
                .values(fidelity=fidelity)
            )

    async def set_isolation(self, run_id: str, owner_epoch: int, isolation: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(control_runs)
                .where(control_runs.c.run_id == run_id, control_runs.c.owner_epoch == owner_epoch)
                .values(isolation=isolation)
            )

    async def finish(
        self, run_id: str, owner_epoch: int, status: RunStatus, error: str | None = None
    ) -> str | None:
        """Records the outcome. A run cancelled meanwhile stays `cancelled`. A run that becomes
        `interrupted` gets its rerun in the same transaction, whose id is returned. Does nothing
        when the run already finished otherwise. Raises `FencedError` when `owner_epoch` is no
        longer the run's: a worker that took the run over, or the owner's restart, finishes it
        and writes its summary, and the stale owner must write neither."""
        runs = control_runs.c
        async with self._engine.begin() as conn:
            found = (
                await conn.execute(
                    select(runs.status, runs.owner_epoch)
                    .where(runs.run_id == run_id)
                    .with_for_update()
                )
            ).one_or_none()
            if found is None:
                raise RunNotFound(f"no run `{run_id}`.")
            current, epoch = found
            if epoch != owner_epoch:
                raise FencedError(
                    f"run {run_id} is at owner_epoch {epoch}, the finish came at {owner_epoch}. "
                    "Another worker took the run over, or its owner restarted; that one records "
                    "its outcome."
                )
            if current not in _FINISHABLE:
                return None
            final: RunStatus = "cancelled" if current == "cancelled" else status
            await conn.execute(
                update(control_runs)
                .where(runs.run_id == run_id)
                .values(status=final, error=error, finished_at=func.now())
            )
            if final == "interrupted":
                return await _rerun(conn, run_id)
            return None

    async def summary_failed(self, run_id: str, owner_epoch: int, error: str) -> RunStatus | None:
        """Records that a finished run's summary could not be written, after `finish`. A `done`
        run becomes `failed`, since no report can see it; any other status stays, so an
        interrupted run keeps the rerun `finish` queued and a cancel still wins. `error` is
        added to the run's error. Returns the status, or `None` when `owner_epoch` is no longer
        the run's."""
        runs = control_runs.c
        async with self._engine.begin() as conn:
            return (
                await conn.execute(
                    update(control_runs)
                    .where(runs.run_id == run_id, runs.owner_epoch == owner_epoch)
                    .values(
                        status=case((runs.status == "done", "failed"), else_=runs.status),
                        error=func.concat_ws("; ", runs.error, error),
                    )
                    .returning(runs.status)
                )
            ).scalar_one_or_none()

    async def interrupt_owned(self, owner_id: str) -> list[Recovered]:
        """Finishes the runs a previous process of this worker left unfinished: running or paused
        ones become `interrupted` and get their reruns in the same transaction; ones cancelled
        while they ran get their `finished_at`. M3 resumes them instead. Both get a new
        `owner_epoch`, so a process of the old owner that is somehow still running them is
        fenced: its next write raises `FencedError`, and its `finish` does nothing.

        Only rows owned by `owner_id` are touched, which is why worker ids must be unique
        (`swarmeval.worker.hold_worker_id`)."""
        runs = control_runs.c
        async with self._engine.begin() as conn:
            interrupted = await conn.execute(
                update(control_runs)
                .where(runs.owner_id == owner_id, runs.status.in_(("running", "paused")))
                .values(
                    status="interrupted",
                    error="the worker that owned the run restarted",
                    finished_at=func.now(),
                    owner_epoch=runs.owner_epoch + 1,
                )
                .returning(runs.run_id)
            )
            recovered = [
                Recovered(run_id, "interrupted", await _rerun(conn, run_id))
                for run_id in sorted(interrupted.scalars())
            ]
            cancelled = await conn.execute(
                update(control_runs)
                .where(
                    runs.owner_id == owner_id,
                    runs.status == "cancelled",
                    runs.finished_at.is_(None),
                )
                .values(finished_at=func.now(), owner_epoch=runs.owner_epoch + 1)
                .returning(runs.run_id)
            )
            recovered += [Recovered(r, "cancelled", None) for r in sorted(cancelled.scalars())]
        return recovered


async def _rerun(conn: AsyncConnection, run_id: str) -> str | None:
    """Queues the rerun of interrupted `run_id` in `conn`'s transaction: same submission, case
    revision, overrides, and variant, at the next unused epoch, so its seed differs too. A
    variant gets at most `epochs` reruns in all, so a broken backend cannot requeue forever;
    past that, returns `None` and the gap shows in reports."""
    s = run_specs.c
    spec = (
        (
            await conn.execute(
                select(
                    s.submission_id,
                    s.case_id,
                    s.case_sha256,
                    s.case_revision_id,
                    s.overrides,
                    s.variant,
                    s.task_args,
                    s.epochs,
                    s.suite,
                    s.forked_from,
                    s.submitted_by,
                    control_runs.c.workspace,
                )
                .join_from(run_specs, control_runs, s.run_id == control_runs.c.run_id)
                .where(s.run_id == run_id)
            )
        )
        .mappings()
        .one()
    )
    # Serializes the reruns of one variant, so two runs interrupted at once take different
    # epochs: under READ COMMITTED each statement after the lock sees the other's commit.
    lock = f"swarmeval.rerun:{spec['submission_id']}:{spec['variant']}"
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock, 0))))
    reruns, last = (
        await conn.execute(
            select(func.count(s.replaces), func.max(s.epoch)).where(
                s.submission_id == spec["submission_id"], s.variant == spec["variant"]
            )
        )
    ).one()
    if reruns >= spec["epochs"] or spec["forked_from"] is not None:
        # A fork is a counterfactual, not an epoch: an interrupted one is forked again by hand.
        return None
    epoch = last + 1
    rerun_id = run_id_of(spec["case_id"], spec["submission_id"], spec["variant"], epoch)
    await conn.execute(
        insert(control_runs).values(run_id=rerun_id, workspace=spec["workspace"], status="queued")
    )
    await conn.execute(
        insert(run_specs).values(
            run_id=rerun_id,
            submission_id=spec["submission_id"],
            case_id=spec["case_id"],
            case_sha256=spec["case_sha256"],
            case_revision_id=spec["case_revision_id"],
            overrides=spec["overrides"],
            variant=spec["variant"],
            task_args=spec["task_args"],
            epoch=epoch,
            epochs=spec["epochs"],
            replaces=run_id,
            suite=spec["suite"],
            submitted_by=spec["submitted_by"],
        )
    )
    return rerun_id
