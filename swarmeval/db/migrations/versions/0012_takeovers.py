"""Takeovers: control.runs `takeovers`

Numbered 0012 because 0011 is taken by a change developed alongside this one (the analysis
service's `analysis.jobs`); whichever of the two lands second revises the other.

Revision ID: 0012
Revises: 0010
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Runs taken over before this column existed are not counted: nothing recorded them.
    op.add_column(
        "runs",
        sa.Column("takeovers", sa.Integer(), server_default="0", nullable=False),
        schema="control",
    )


def downgrade() -> None:
    op.drop_column("runs", "takeovers", schema="control")
