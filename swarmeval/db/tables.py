"""The `control`, `runs`, and `analysis` schemas, as SQLAlchemy Core tables.

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
DELIVERY_STATUSES = ("pending", "delivered")
VERDICT_STATUSES = ("accepted", "rejected")
VERDICT_ANSWERS = ("yes", "no", "unclear")

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
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("isolation", Text),
    Column("error", Text),
    CheckConstraint("status IN (" + ", ".join(f"'{s}'" for s in RUN_STATUSES) + ")", name="status"),
    Index(None, "status", "created_at"),
    schema="control",
)

run_specs = Table(
    "run_specs",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("submission_id", Text, nullable=False),
    Column("case_id", Text, nullable=False),
    Column("case_sha256", Text, nullable=False),
    Column("overrides", JSONB, nullable=False),
    Column("variant", Integer, nullable=False),
    Column("task_args", JSONB, nullable=False),
    Column("epoch", Integer, nullable=False),
    Column("epochs", Integer, nullable=False),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    Index(None, "submission_id"),
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

deliveries = Table(
    "deliveries",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("msg_seq", BigInteger, primary_key=True),
    Column("recipient", Text, primary_key=True),
    Column("status", Text, nullable=False),
    Column("delivered_seq", BigInteger),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{s}'" for s in DELIVERY_STATUSES) + ")", name="status"
    ),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    schema="runs",
)

judge_verdicts = Table(
    "judge_verdicts",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("question", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("from_seq", BigInteger),
    Column("to_seq", BigInteger),
    Column("status", Text, nullable=False),
    Column("answer", Text),
    Column("explanation", Text),
    Column("citations", JSONB, nullable=False),
    Column("rejection", Text),
    Column("request", JSONB, nullable=False),
    Column("response", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{s}'" for s in VERDICT_STATUSES) + ")", name="status"
    ),
    CheckConstraint(
        "answer IN (" + ", ".join(f"'{a}'" for a in VERDICT_ANSWERS) + ")", name="answer"
    ),
    Index(None, "run_id"),
    schema="analysis",
)
"""Owned by analysis: one row per judge call, accepted or not. No foreign key to `control.runs`:
analysis reads exports, not the orchestrator's tables, and its rows outlive a run's cleanup."""
