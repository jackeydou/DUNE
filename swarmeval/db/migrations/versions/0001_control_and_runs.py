"""control.runs, and the runs schema's events, messages, agent_state, extension_state

Revision ID: 0001
Revises:
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS control")
    op.execute("CREATE SCHEMA IF NOT EXISTS runs")
    op.create_table(
        "runs",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_epoch", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'interrupted', 'done', 'failed', "
            "'cancelled')",
            name=op.f("ck_runs_status"),
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_runs")),
        schema="control",
    )
    op.create_table(
        "events",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("sandbox_id", sa.Text(), nullable=True),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("prev_hash", sa.LargeBinary(), nullable=False),
        sa.Column("hash", sa.LargeBinary(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["control.runs.run_id"], name=op.f("fk_events_run_id")),
        sa.PrimaryKeyConstraint("run_id", "seq", name=op.f("pk_events")),
        sa.UniqueConstraint("event_id", name=op.f("uq_events_event_id")),
        schema="runs",
    )
    op.create_table(
        "messages",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("gen", sa.Integer(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("message", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["control.runs.run_id"], name=op.f("fk_messages_run_id")
        ),
        sa.PrimaryKeyConstraint("run_id", "agent_id", "gen", "idx", name=op.f("pk_messages")),
        schema="runs",
    )
    op.create_table(
        "agent_state",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("gen", sa.Integer(), nullable=False),
        sa.Column("len", sa.Integer(), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tokens_used", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["control.runs.run_id"], name=op.f("fk_agent_state_run_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_state")),
        schema="runs",
    )
    op.create_index(
        op.f("ix_agent_state_run_id_agent_id_seq_id"),
        "agent_state",
        ["run_id", "agent_id", "seq", "id"],
        schema="runs",
    )
    op.create_table(
        "extension_state",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("state", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["control.runs.run_id"], name=op.f("fk_extension_state_run_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extension_state")),
        schema="runs",
    )
    op.create_index(
        op.f("ix_extension_state_run_id_instance_id_seq_id"),
        "extension_state",
        ["run_id", "instance_id", "seq", "id"],
        schema="runs",
    )


def downgrade() -> None:
    op.drop_table("extension_state", schema="runs")
    op.drop_table("agent_state", schema="runs")
    op.drop_table("messages", schema="runs")
    op.drop_table("events", schema="runs")
    op.drop_table("runs", schema="control")
    op.execute("DROP SCHEMA runs")
    op.execute("DROP SCHEMA control")
