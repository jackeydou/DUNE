import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from swarmeval.db import metadata, sync_engine

pytestmark = pytest.mark.docker


def test_migrations_produce_exactly_the_declared_tables(postgres_url: str) -> None:
    engine = sync_engine(postgres_url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"include_schemas": True})
            diff = compare_metadata(context, metadata)
    finally:
        engine.dispose()

    assert diff == []
