"""control.run_specs, and run timing, isolation, and error on control.runs

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("started_at", "finished_at"):
        op.add_column("runs", sa.Column(name, sa.DateTime(timezone=True)), schema="control")
    for name in ("isolation", "error"):
        op.add_column("runs", sa.Column(name, sa.Text()), schema="control")
    op.create_index(
        op.f("ix_runs_status_created_at"), "runs", ["status", "created_at"], schema="control"
    )
    op.create_table(
        "run_specs",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("submission_id", sa.Text(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("case_sha256", sa.Text(), nullable=False),
        sa.Column("overrides", JSONB(), nullable=False),
        sa.Column("variant", sa.Integer(), nullable=False),
        sa.Column("task_args", JSONB(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("epochs", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["control.runs.run_id"], name=op.f("fk_run_specs_run_id")
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_run_specs")),
        schema="control",
    )
    op.create_index(
        op.f("ix_run_specs_submission_id"), "run_specs", ["submission_id"], schema="control"
    )


def downgrade() -> None:
    op.drop_table("run_specs", schema="control")
    op.drop_index(op.f("ix_runs_status_created_at"), "runs", schema="control")
    for name in ("error", "isolation", "finished_at", "started_at"):
        op.drop_column("runs", name, schema="control")
