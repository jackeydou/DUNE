"""Throwaway Postgres and RustFS for tests marked `docker`, started once per session."""

from collections.abc import AsyncIterator, Iterator
from itertools import count

import pytest
from pyarrow.fs import S3FileSystem
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy

from swarmeval.db import async_engine, control_runs, migrate
from swarmeval.events import ObjectStore

POSTGRES_IMAGE = "postgres:18-alpine"
RUSTFS_IMAGE = "rustfs/rustfs:latest"
_RUSTFS_KEY = "swarmeval-test"
_run_ids = count(1)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver=None) as container:
        url = container.get_connection_url()
        migrate(url)
        yield url


@pytest.fixture(scope="session")
def object_store() -> Iterator[ObjectStore]:
    """A RustFS with an empty `exports` bucket."""
    container = (
        DockerContainer(RUSTFS_IMAGE)
        .with_exposed_ports(9000)
        .with_env("RUSTFS_ACCESS_KEY", _RUSTFS_KEY)
        .with_env("RUSTFS_SECRET_KEY", _RUSTFS_KEY)
        # Unauthenticated S3 requests get 403 once the server is up.
        .waiting_for(HttpWaitStrategy(9000, "/").for_status_code_matching(lambda c: c < 500))
    )
    with container:
        endpoint = f"{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"
        store = ObjectStore(
            endpoint=endpoint,
            bucket="exports",
            access_key=_RUSTFS_KEY,
            secret_key=_RUSTFS_KEY,
            scheme="http",
        )
        admin = S3FileSystem(
            access_key=_RUSTFS_KEY,
            secret_key=_RUSTFS_KEY,
            endpoint_override=endpoint,
            scheme="http",
            region=store.region,
            allow_bucket_creation=True,
        )
        admin.create_dir(store.bucket)
        yield store


async def create_run(engine: AsyncEngine, run_id: str, *, owner_epoch: int = 1) -> None:
    """Stands in for the control plane's enqueue and a worker's claim."""
    async with engine.begin() as conn:
        await conn.execute(
            insert(control_runs).values(
                run_id=run_id, workspace="ws_test", status="running", owner_epoch=owner_epoch
            )
        )


@pytest.fixture
async def engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = async_engine(postgres_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def run_id(engine: AsyncEngine) -> str:
    """A fresh run with a `control.runs` row at owner_epoch 1."""
    run_id = f"run_{next(_run_ids)}"
    await create_run(engine, run_id)
    return run_id
