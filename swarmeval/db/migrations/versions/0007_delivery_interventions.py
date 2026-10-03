"""runs.deliveries `dropped` / `delayed` with `due_turn`, and runs.extension_state `rng_uses`

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("deliveries", sa.Column("due_turn", sa.Integer()), schema="runs")
    op.drop_constraint(op.f("ck_deliveries_status"), "deliveries", schema="runs")
    op.create_check_constraint(
        op.f("ck_deliveries_status"),
        "deliveries",
        "status IN ('pending', 'delivered', 'dropped', 'delayed')",
        schema="runs",
    )
    op.add_column(
        "extension_state",
        sa.Column("rng_uses", sa.BigInteger(), server_default="0", nullable=False),
        schema="runs",
    )


def downgrade() -> None:
    op.drop_column("extension_state", "rng_uses", schema="runs")
    op.drop_constraint(op.f("ck_deliveries_status"), "deliveries", schema="runs")
    op.create_check_constraint(
        op.f("ck_deliveries_status"),
        "deliveries",
        "status IN ('pending', 'delivered')",
        schema="runs",
    )
    op.drop_column("deliveries", "due_turn", schema="runs")
