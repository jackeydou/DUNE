"""analysis.jobs: background jobs of the analysis service

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("actor", sa.Text()),
        sa.Column("request", JSONB(), nullable=False),
        sa.Column("result", JSONB()),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')", name=op.f("ck_jobs_status")
        ),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_jobs")),
        schema="analysis",
    )


def downgrade() -> None:
    op.drop_table("jobs", schema="analysis")
