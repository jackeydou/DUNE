"""The `control` and `runs` schemas, as SQLAlchemy Core tables.

Layout and invariants are documented in docs/event-log.md#tables. Migrations under
`migrations/versions/` must produce exactly these tables; `tests/db/test_migrations.py` checks.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

RUN_STATUSES = ("queued", "running", "paused", "interrupted", "done", "failed", "cancelled")

metadata = MetaData(
    naming_convention={
        "pk": "pk_%(table_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    }
)

control_runs = Table(
    "runs",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("workspace", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("owner_id", Text),
    Column("lease_until", DateTime(timezone=True)),
    Column("owner_epoch", BigInteger, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status IN (" + ", ".join(f"'{s}'" for s in RUN_STATUSES) + ")", name="status"),
    schema="control",
)

events = Table(
    "events",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("seq", BigInteger, primary_key=True),
    Column("event_id", Text, nullable=False, unique=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("type", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("agent_id", Text),
    Column("sandbox_id", Text),
    Column("parent_id", Text),
    Column("prev_hash", LargeBinary, nullable=False),
    Column("hash", LargeBinary, nullable=False),
    Column("payload", JSONB, nullable=False),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    schema="runs",
)

messages = Table(
    "messages",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("agent_id", Text, primary_key=True),
    Column("gen", Integer, primary_key=True),
    Column("idx", Integer, primary_key=True),
    Column("seq", BigInteger, nullable=False),
    Column("message", JSONB, nullable=False),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    schema="runs",
)

agent_state = Table(
    "agent_state",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("gen", Integer, nullable=False),
    Column("len", Integer, nullable=False),
    Column("turn", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("tokens_used", BigInteger, nullable=False),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    Index(None, "run_id", "agent_id", "seq", "id"),
    schema="runs",
)

extension_state = Table(
    "extension_state",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("instance_id", Text, nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("state", JSONB, nullable=False),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    Index(None, "run_id", "instance_id", "seq", "id"),
    schema="runs",
)
