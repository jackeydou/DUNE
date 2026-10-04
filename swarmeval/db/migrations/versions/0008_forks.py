"""Forks: control.run_specs `forked_from`, `fork_seq`, `fork_edits`; control.runs `fidelity`;
runs.checkpoints, the loop's state at each turn start; runs.canaries, a run's canary tokens

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run_specs", sa.Column("forked_from", sa.Text()), schema="control")
    op.add_column("run_specs", sa.Column("fork_seq", sa.BigInteger()), schema="control")
    op.add_column(
        "run_specs",
        sa.Column("fork_edits", postgresql.JSONB(astext_type=sa.Text())),
        schema="control",
    )
    op.create_index(
        op.f("ix_run_specs_forked_from"), "run_specs", ["forked_from"], schema="control"
    )
    op.add_column("runs", sa.Column("fidelity", sa.Text()), schema="control")
    op.create_table(
        "checkpoints",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["control.runs.run_id"], name=op.f("fk_checkpoints_run_id")
        ),
        sa.PrimaryKeyConstraint("run_id", "turn", name=op.f("pk_checkpoints")),
        schema="runs",
    )
    op.create_index(
        op.f("ix_checkpoints_run_id_seq"), "checkpoints", ["run_id", "seq"], schema="runs"
    )
    op.create_table(
        "canaries",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("canaries", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sandbox_canaries", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["control.runs.run_id"], name=op.f("fk_canaries_run_id")
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_canaries")),
        schema="runs",
    )


def downgrade() -> None:
    op.drop_table("canaries", schema="runs")
    op.drop_index(op.f("ix_checkpoints_run_id_seq"), "checkpoints", schema="runs")
    op.drop_table("checkpoints", schema="runs")
    op.drop_column("runs", "fidelity", schema="control")
    op.drop_index(op.f("ix_run_specs_forked_from"), "run_specs", schema="control")
    op.drop_column("run_specs", "fork_edits", schema="control")
    op.drop_column("run_specs", "fork_seq", schema="control")
    op.drop_column("run_specs", "forked_from", schema="control")
