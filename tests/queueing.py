"""Queue fixtures for tests marked `docker`: submissions enqueued straight into the shared
Postgres, with no case bundle behind them."""

import secrets
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.cases import add_revision
from swarmeval.control.queue import NewRun, Queue, run_id_of
from swarmeval.db import control_runs, run_specs


@pytest.fixture
async def submission(engine: AsyncEngine) -> AsyncIterator[str]:
    """A fresh submission id. Its runs still queued at teardown are cancelled, so no later test
    claims them from the shared database."""
    submission_id = secrets.token_hex(4)
    yield submission_id
    mine = select(run_specs.c.run_id).where(run_specs.c.submission_id == submission_id)
    async with engine.begin() as conn:
        await conn.execute(
            update(control_runs)
            .where(control_runs.c.status == "queued", control_runs.c.run_id.in_(mine))
            .values(status="cancelled")
        )


async def revision_id(engine: AsyncEngine, case_id: str, sha256: str = "ab" * 32) -> int:
    """The library revision of `case_id` in `ws_test` that names bundle `sha256`, added if it
    is not the case's newest."""
    async with engine.begin() as conn:
        revision, _ = await add_revision(
            conn, workspace="ws_test", case_id=case_id, sha256=sha256, actor=None, note=None
        )
    return revision.id


async def enqueue(
    queue: Queue,
    engine: AsyncEngine,
    submission_id: str,
    *,
    variants: int = 1,
    epochs: int = 1,
    case_id: str = "c",
    suite: str | None = None,
    submitted_by: str | None = None,
) -> list[str]:
    case_revision_id = await revision_id(engine, case_id)
    runs = [
        NewRun(
            run_id=run_id_of(case_id, submission_id, variant, epoch),
            submission_id=submission_id,
            case_id=case_id,
            workspace="ws_test",
            case_sha256="ab" * 32,
            case_revision_id=case_revision_id,
            overrides={},
            variant=variant,
            task_args={"v": variant},
            epoch=epoch,
            epochs=epochs,
            suite=suite,
            submitted_by=submitted_by,
        )
        for variant in range(variants)
        for epoch in range(1, epochs + 1)
    ]
    await queue.enqueue(runs)
    return [r.run_id for r in runs]


async def start(engine: AsyncEngine, run_id: str, owner_id: str) -> int:
    """Claims one particular run for `owner_id`, as `Queue.claim` would; returns the new
    `owner_epoch`."""
    runs = control_runs.c
    async with engine.begin() as conn:
        return (
            await conn.execute(
                update(control_runs)
                .where(runs.run_id == run_id, runs.status == "queued")
                .values(status="running", owner_id=owner_id, owner_epoch=runs.owner_epoch + 1)
                .returning(runs.owner_epoch)
            )
        ).scalar_one()


async def lease(engine: AsyncEngine, run_id: str, owner_id: str, seconds: float) -> int:
    """Claims one particular run for `owner_id` with a lease of `seconds` from now, which may be
    negative for one that has already run out; returns the new `owner_epoch`."""
    runs = control_runs.c
    async with engine.begin() as conn:
        return (
            await conn.execute(
                update(control_runs)
                .where(runs.run_id == run_id, runs.status == "queued")
                .values(
                    status="running",
                    owner_id=owner_id,
                    owner_epoch=runs.owner_epoch + 1,
                    lease_until=func.now() + timedelta(seconds=seconds),
                )
                .returning(runs.owner_epoch)
            )
        ).scalar_one()
