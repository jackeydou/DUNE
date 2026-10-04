"""Case library: control.cases, control.case_revisions; control.run_specs `case_revision_id`

Runs queued before the library existed are given cases and revisions: one case per workspace
and case id, and one revision per bundle hash, numbered in the order the hashes were first used.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL_NOTE = "From runs submitted before the case library."

_FIRST_USES = """
    SELECT r.workspace, s.case_id, s.case_sha256,
           min(r.created_at) AS first_used,
           (array_agg(s.submitted_by ORDER BY r.created_at, r.run_id))[1] AS actor
    FROM control.run_specs s JOIN control.runs r ON r.run_id = s.run_id
    GROUP BY r.workspace, s.case_id, s.case_sha256
"""


def upgrade() -> None:
    op.create_table(
        "cases",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cases")),
        sa.UniqueConstraint("workspace", "case_id", name=op.f("uq_cases_workspace")),
        schema="control",
    )
    op.create_table(
        "case_revisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("case_pk", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("bundle_sha256", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["case_pk"], ["control.cases.id"], name=op.f("fk_case_revisions_case_pk")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_revisions")),
        sa.UniqueConstraint("case_pk", "revision", name=op.f("uq_case_revisions_case_pk")),
        schema="control",
    )
    op.add_column("run_specs", sa.Column("case_revision_id", sa.BigInteger()), schema="control")

    op.execute(
        f"""
        INSERT INTO control.cases (workspace, case_id, created_at)
        SELECT workspace, case_id, min(first_used) FROM ({_FIRST_USES}) AS used
        GROUP BY workspace, case_id
        """
    )
    op.get_bind().execute(
        sa.text(
            f"""
            INSERT INTO control.case_revisions
                (case_pk, revision, bundle_sha256, actor, note, created_at)
            SELECT c.id,
                   row_number() OVER (
                       PARTITION BY c.id ORDER BY used.first_used, used.case_sha256
                   ),
                   used.case_sha256, used.actor, :note, used.first_used
            FROM ({_FIRST_USES}) AS used
            JOIN control.cases c ON c.workspace = used.workspace AND c.case_id = used.case_id
            """
        ),
        {"note": BACKFILL_NOTE},
    )
    op.execute(
        """
        UPDATE control.run_specs s SET case_revision_id = v.id
        FROM control.runs r, control.cases c, control.case_revisions v
        WHERE r.run_id = s.run_id AND c.workspace = r.workspace AND c.case_id = s.case_id
          AND v.case_pk = c.id AND v.bundle_sha256 = s.case_sha256
        """
    )

    op.alter_column("run_specs", "case_revision_id", nullable=False, schema="control")
    op.create_foreign_key(
        op.f("fk_run_specs_case_revision_id"),
        "run_specs",
        "case_revisions",
        ["case_revision_id"],
        ["id"],
        source_schema="control",
        referent_schema="control",
    )
    op.create_index(
        op.f("ix_run_specs_case_revision_id"), "run_specs", ["case_revision_id"], schema="control"
    )


def downgrade() -> None:
    op.drop_column("run_specs", "case_revision_id", schema="control")
    op.drop_table("case_revisions", schema="control")
    op.drop_table("cases", schema="control")
