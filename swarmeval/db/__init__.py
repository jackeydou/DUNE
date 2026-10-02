"""The orchestrator's Postgres: the `control` and `runs` schemas, engines, and migrations.

Table layout: docs/event-log.md#tables. Only the orchestrator connects to this database.
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from swarmeval.db.tables import (
    DELIVERY_STATUSES,
    RUN_STATUSES,
    VERDICT_ANSWERS,
    VERDICT_STATUSES,
    agent_state,
    control_runs,
    deliveries,
    events,
    extension_state,
    judge_verdicts,
    messages,
    metadata,
    run_specs,
)

__all__ = [
    "DELIVERY_STATUSES",
    "RUN_STATUSES",
    "VERDICT_ANSWERS",
    "VERDICT_STATUSES",
    "agent_state",
    "async_engine",
    "control_runs",
    "deliveries",
    "events",
    "extension_state",
    "judge_verdicts",
    "messages",
    "metadata",
    "migrate",
    "run_specs",
    "sync_engine",
]


def _psycopg_url(url: str) -> str:
    """Accepts a libpq-style `postgresql://` URL and selects the psycopg 3 driver."""
    return make_url(url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def async_engine(url: str) -> AsyncEngine:
    return create_async_engine(_psycopg_url(url))


def sync_engine(url: str) -> Engine:
    return create_engine(_psycopg_url(url))


def migrate(url: str, revision: str = "head") -> None:
    """Brings the database to `revision`, creating the schemas on first use."""
    config = Config()
    config.set_main_option("script_location", "swarmeval.db:migrations")
    config.set_main_option("path_separator", "os")
    engine = sync_engine(url)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
    finally:
        engine.dispose()
