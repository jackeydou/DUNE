"""Actors: control.run_specs `submitted_by`; control.runs `cancelled_by`, `resumed_by`

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run_specs", sa.Column("submitted_by", sa.Text()), schema="control")
    op.add_column("runs", sa.Column("cancelled_by", sa.Text()), schema="control")
    op.add_column("runs", sa.Column("resumed_by", sa.Text()), schema="control")


def downgrade() -> None:
    op.drop_column("runs", "resumed_by", schema="control")
    op.drop_column("runs", "cancelled_by", schema="control")
    op.drop_column("run_specs", "submitted_by", schema="control")
