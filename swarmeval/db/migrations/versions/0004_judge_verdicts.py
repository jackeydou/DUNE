"""analysis.judge_verdicts: one row per LLM judge call

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS analysis")
    op.create_table(
        "judge_verdicts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("from_seq", sa.BigInteger(), nullable=True),
        sa.Column("to_seq", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("citations", JSONB(), nullable=False),
        sa.Column("rejection", sa.Text(), nullable=True),
        sa.Column("request", JSONB(), nullable=False),
        sa.Column("response", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'rejected')", name=op.f("ck_judge_verdicts_status")
        ),
        sa.CheckConstraint(
            "answer IN ('yes', 'no', 'unclear')", name=op.f("ck_judge_verdicts_answer")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_judge_verdicts")),
        schema="analysis",
    )
    op.create_index(
        op.f("ix_judge_verdicts_run_id"), "judge_verdicts", ["run_id"], schema="analysis"
    )


def downgrade() -> None:
    op.drop_table("judge_verdicts", schema="analysis")
    op.execute("DROP SCHEMA IF EXISTS analysis")
