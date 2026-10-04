"""Migration 0010 on a database that already holds runs: every run gets a case library
revision (M4 spec decision 6)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import make_url, text

from swarmeval.db import migrate, sync_engine

pytestmark = pytest.mark.docker

T0 = datetime(2026, 9, 1, tzinfo=UTC)
A, B, C = "aa" * 32, "bb" * 32, "cc" * 32

OLD_RUNS = [
    # run_id, workspace, case_id, case_sha256, submitted_by, minutes after T0
    ("demo.s1.v0.e1", "safety", "demo", A, "ada", 0),
    ("demo.s1.v0.e2", "safety", "demo", A, "ada", 1),
    ("demo.s2.v0.e1", "safety", "demo", B, "bob", 2),
    ("demo.s3.v0.e1", "safety", "demo", A, "cy", 3),
    ("demo.s4.v0.e1", "other", "demo", C, None, 4),
    ("probe.s5.v0.e1", "safety", "probe", A, None, 5),
]


def test_runs_from_before_the_library_get_cases_and_revisions(postgres_url: str) -> None:
    admin = sync_engine(postgres_url)
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("DROP DATABASE IF EXISTS backfill"))
        conn.execute(text("CREATE DATABASE backfill"))
    admin.dispose()
    url = make_url(postgres_url).set(database="backfill").render_as_string(hide_password=False)
    migrate(url, "0009")
    engine = sync_engine(url)
    try:
        with engine.begin() as conn:
            for run_id, workspace, case_id, sha256, actor, minute in OLD_RUNS:
                conn.execute(
                    text(
                        "INSERT INTO control.runs (run_id, workspace, status, created_at) "
                        "VALUES (:run_id, :workspace, 'done', :at)"
                    ),
                    {
                        "run_id": run_id,
                        "workspace": workspace,
                        "at": T0 + timedelta(minutes=minute),
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO control.run_specs (run_id, submission_id, case_id, "
                        "case_sha256, overrides, variant, task_args, epoch, epochs, submitted_by) "
                        "VALUES (:run_id, 's', :case_id, :sha256, '{}', 0, '{}', 1, 1, :actor)"
                    ),
                    {"run_id": run_id, "case_id": case_id, "sha256": sha256, "actor": actor},
                )

        migrate(url)

        with engine.connect() as conn:
            revisions = conn.execute(
                text(
                    "SELECT c.workspace, c.case_id, v.revision, v.bundle_sha256, v.actor, "
                    "v.created_at, c.created_at FROM control.case_revisions v "
                    "JOIN control.cases c ON c.id = v.case_pk ORDER BY 1, 2, 3"
                )
            ).all()
            runs = dict(
                conn.execute(
                    text(
                        "SELECT s.run_id, c.workspace || '/' || c.case_id || '@' || v.revision "
                        "FROM control.run_specs s "
                        "JOIN control.case_revisions v ON v.id = s.case_revision_id "
                        "JOIN control.cases c ON c.id = v.case_pk"
                    )
                ).all()
            )
    finally:
        engine.dispose()

    def at(minute: int) -> datetime:
        return T0 + timedelta(minutes=minute)

    # One revision per hash, numbered by first use; a hash used again later adds none.
    assert [tuple(r) for r in revisions] == [
        ("other", "demo", 1, C, None, at(4), at(4)),
        ("safety", "demo", 1, A, "ada", at(0), at(0)),
        ("safety", "demo", 2, B, "bob", at(2), at(0)),
        ("safety", "probe", 1, A, None, at(5), at(5)),
    ]
    assert runs == {
        "demo.s1.v0.e1": "safety/demo@1",
        "demo.s1.v0.e2": "safety/demo@1",
        "demo.s2.v0.e1": "safety/demo@2",
        "demo.s3.v0.e1": "safety/demo@1",
        "demo.s4.v0.e1": "other/demo@1",
        "probe.s5.v0.e1": "safety/probe@1",
    }
