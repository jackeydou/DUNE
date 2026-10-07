"""Run models: control.run_specs `models`

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Runs queued before this column existed named their models in their case; they have none.
    op.add_column(
        "run_specs",
        sa.Column("models", postgresql.JSONB(astext_type=sa.Text())),
        schema="control",
    )


def downgrade() -> None:
    op.drop_column("run_specs", "models", schema="control")
