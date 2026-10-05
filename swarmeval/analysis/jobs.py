"""Background jobs of the analysis service: rows in `analysis.jobs`
(docs/services/analysis.md#jobs).

A job runs in the process that took it. `fail_unfinished` at start marks the ones a stopped
process left behind, so no job stays `running` with nothing running it.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import JsonValue
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.db import analysis_jobs

JobStatus = Literal["queued", "running", "done", "failed"]


class JobNotFound(Exception):
    pass


@dataclass(frozen=True)
class JobRow:
    job_id: str
    kind: str
    status: JobStatus
    actor: str | None
    request: dict[str, JsonValue]
    result: dict[str, JsonValue] | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class Jobs:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, kind: str, actor: str | None, request: dict[str, JsonValue]) -> JobRow:
        job_id = secrets.token_hex(8)
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(analysis_jobs).values(job_id=job_id, kind=kind, actor=actor, request=request)
            )
        return await self.get(job_id)

    async def get(self, job_id: str) -> JobRow:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(select(analysis_jobs).where(analysis_jobs.c.job_id == job_id))
            ).one_or_none()
        if row is None:
            raise JobNotFound(f"no analysis job `{job_id}`.")
        # `_asdict` is public API; the underscore only avoids clashing with column names.
        return JobRow(**row._asdict())  # pyright: ignore[reportPrivateUsage]

    async def running(self, job_id: str) -> None:
        await self._set(job_id, status="running")

    async def done(self, job_id: str, result: dict[str, JsonValue]) -> None:
        await self._set(job_id, status="done", result=result, finished_at=func.now())

    async def failed(self, job_id: str, error: str) -> None:
        await self._set(job_id, status="failed", error=error, finished_at=func.now())

    async def _set(self, job_id: str, **values: object) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(analysis_jobs).where(analysis_jobs.c.job_id == job_id).values(**values)
            )

    async def fail_unfinished(self) -> int:
        """Marks every `queued` or `running` job `failed`. Call it once, before the service
        takes jobs: whatever is unfinished then belonged to a process that stopped."""
        async with self._engine.begin() as conn:
            marked = await conn.execute(
                update(analysis_jobs)
                .where(analysis_jobs.c.status.in_(("queued", "running")))
                .values(
                    status="failed",
                    error="the analysis service restarted while the job ran. Start it again.",
                    finished_at=func.now(),
                )
            )
            return marked.rowcount
