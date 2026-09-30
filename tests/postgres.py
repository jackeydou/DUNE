"""A throwaway Postgres for tests marked `docker`, migrated once per session."""

from collections.abc import Iterator

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from swarmeval.db import control_runs, migrate

IMAGE = "postgres:18-alpine"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    with PostgresContainer(IMAGE, driver=None) as container:
        url = container.get_connection_url()
        migrate(url)
        yield url


async def create_run(engine: AsyncEngine, run_id: str, *, owner_epoch: int = 1) -> None:
    """Stands in for the control plane's enqueue and a worker's claim."""
    async with engine.begin() as conn:
        await conn.execute(
            insert(control_runs).values(
                run_id=run_id, workspace="ws_test", status="running", owner_epoch=owner_epoch
            )
        )
