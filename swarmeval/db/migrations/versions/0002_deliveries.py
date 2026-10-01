"""runs.deliveries: one row per message recipient

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deliveries",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("msg_seq", sa.BigInteger(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("delivered_seq", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'delivered')", name=op.f("ck_deliveries_status")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["control.runs.run_id"], name=op.f("fk_deliveries_run_id")
        ),
        sa.PrimaryKeyConstraint("run_id", "msg_seq", "recipient", name=op.f("pk_deliveries")),
        schema="runs",
    )


def downgrade() -> None:
    op.drop_table("deliveries", schema="runs")
