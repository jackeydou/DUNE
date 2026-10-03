"""control.run_specs.replaces and suite: the interrupted run a rerun replaces, and the suite
run a submission belongs to

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run_specs", sa.Column("replaces", sa.Text()), schema="control")
    op.create_index(op.f("ix_run_specs_replaces"), "run_specs", ["replaces"], schema="control")
    op.add_column("run_specs", sa.Column("suite", sa.Text()), schema="control")
    op.create_index(op.f("ix_run_specs_suite"), "run_specs", ["suite"], schema="control")


def downgrade() -> None:
    op.drop_index(op.f("ix_run_specs_suite"), "run_specs", schema="control")
    op.drop_column("run_specs", "suite", schema="control")
    op.drop_index(op.f("ix_run_specs_replaces"), "run_specs", schema="control")
    op.drop_column("run_specs", "replaces", schema="control")
