"""analysis.rule_scans and analysis.rule_matches: rule scans over exported runs

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rule_scans",
        sa.Column("rule_set_sha256", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("rules", JSONB(), nullable=False),
        sa.Column("matches", sa.Integer(), nullable=False),
        sa.Column(
            "scanned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("rule_set_sha256", "run_id", name=op.f("pk_rule_scans")),
        schema="analysis",
    )
    op.create_index(op.f("ix_rule_scans_run_id"), "rule_scans", ["run_id"], schema="analysis")
    op.create_table(
        "rule_matches",
        sa.Column("rule_set_sha256", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("via", JSONB(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["rule_set_sha256", "run_id"],
            ["analysis.rule_scans.rule_set_sha256", "analysis.rule_scans.run_id"],
            name=op.f("fk_rule_matches_rule_set_sha256"),
        ),
        sa.PrimaryKeyConstraint(
            "rule_set_sha256", "run_id", "event_id", "rule_id", name=op.f("pk_rule_matches")
        ),
        schema="analysis",
    )
    op.create_index(
        op.f("ix_rule_matches_run_id_event_id"),
        "rule_matches",
        ["run_id", "event_id"],
        schema="analysis",
    )


def downgrade() -> None:
    op.drop_table("rule_matches", schema="analysis")
    op.drop_table("rule_scans", schema="analysis")
