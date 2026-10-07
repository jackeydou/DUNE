"""The `control`, `runs`, and `analysis` schemas, as SQLAlchemy Core tables.

Layout and invariants are documented in docs/event-log.md#tables. Migrations under
`migrations/versions/` must produce exactly these tables; `tests/db/test_migrations.py` checks.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

RUN_STATUSES = ("queued", "running", "paused", "interrupted", "done", "failed", "cancelled")
DELIVERY_STATUSES = ("pending", "delivered", "dropped", "delayed")
VERDICT_STATUSES = ("accepted", "rejected")
VERDICT_ANSWERS = ("yes", "no", "unclear")
JOB_STATUSES = ("queued", "running", "done", "failed")

metadata = MetaData(
    naming_convention={
        "pk": "pk_%(table_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    }
)

control_runs = Table(
    "runs",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("workspace", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("owner_id", Text),
    Column("lease_until", DateTime(timezone=True)),
    Column("owner_epoch", BigInteger, nullable=False, server_default="0"),
    Column("takeovers", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("isolation", Text),
    Column("fidelity", Text),
    Column("error", Text),
    Column("cancelled_by", Text),
    Column("resumed_by", Text),
    CheckConstraint("status IN (" + ", ".join(f"'{s}'" for s in RUN_STATUSES) + ")", name="status"),
    Index(None, "status", "created_at"),
    schema="control",
)

cases = Table(
    "cases",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("workspace", Text, nullable=False),
    Column("case_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("archived_at", DateTime(timezone=True)),
    UniqueConstraint("workspace", "case_id"),
    schema="control",
)
"""The case library: one row per `case.yaml` `workspace` and `id`. An archived case takes no
pushes, edits, or runs (docs/services/orchestrator.md#case-library; migration 0010)."""

case_revisions = Table(
    "case_revisions",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("case_pk", BigInteger, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("bundle_sha256", Text, nullable=False),
    Column("actor", Text),
    Column("note", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(["case_pk"], [cases.c.id]),
    UniqueConstraint("case_pk", "revision"),
    schema="control",
)
"""A case's revisions, numbered from 1. A row is never updated or deleted: runs reference it.
`bundle_sha256` names the bundle in object storage; two revisions of a case may share one, when
the case went back to earlier content."""

run_specs = Table(
    "run_specs",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("submission_id", Text, nullable=False),
    Column("case_id", Text, nullable=False),
    Column("case_sha256", Text, nullable=False),
    Column("case_revision_id", BigInteger, nullable=False),
    Column("overrides", JSONB, nullable=False),
    Column("variant", Integer, nullable=False),
    Column("task_args", JSONB, nullable=False),
    Column("epoch", Integer, nullable=False),
    Column("epochs", Integer, nullable=False),
    Column("replaces", Text),
    Column("suite", Text),
    Column("forked_from", Text),
    Column("fork_seq", BigInteger),
    Column("fork_edits", JSONB),
    Column("submitted_by", Text),
    Column("models", JSONB),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    ForeignKeyConstraint(["case_revision_id"], [case_revisions.c.id]),
    Index(None, "case_revision_id"),
    Index(None, "submission_id"),
    Index(None, "replaces"),
    Index(None, "suite"),
    Index(None, "forked_from"),
    schema="control",
)
"""`case_revision_id` is the case library revision the run uses, and `case_sha256` that revision's
bundle. `replaces` is the interrupted run a rerun stands in for, at the next unused epoch of the
same submission and variant (docs/services/orchestrator.md#reruns). `suite` labels the
submissions of one suite run (docs/case-format.md#suites). `forked_from`, `fork_seq`, and
`fork_edits` make a fork: a run that goes on from its source's state after event `fork_seq`, with
the edits applied (docs/services/orchestrator.md#forks; migration 0008). `models` is the run's
model for each of its case's model slots, chosen when it was submitted (a fork's may replace
some); null for runs queued before migration 0013, whose cases named their models."""

events = Table(
    "events",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("seq", BigInteger, primary_key=True),
    Column("event_id", Text, nullable=False, unique=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("type", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("agent_id", Text),
    Column("sandbox_id", Text),
    Column("parent_id", Text),
    Column("prev_hash", LargeBinary, nullable=False),
    Column("hash", LargeBinary, nullable=False),
    Column("payload", JSONB, nullable=False),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    schema="runs",
)

messages = Table(
    "messages",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("agent_id", Text, primary_key=True),
    Column("gen", Integer, primary_key=True),
    Column("idx", Integer, primary_key=True),
    Column("seq", BigInteger, nullable=False),
    Column("message", JSONB, nullable=False),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    schema="runs",
)

agent_state = Table(
    "agent_state",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("gen", Integer, nullable=False),
    Column("len", Integer, nullable=False),
    Column("turn", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("tokens_used", BigInteger, nullable=False),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    Index(None, "run_id", "agent_id", "seq", "id"),
    schema="runs",
)

extension_state = Table(
    "extension_state",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("instance_id", Text, nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("state", JSONB, nullable=False),
    Column("rng_uses", BigInteger, nullable=False, server_default="0"),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    Index(None, "run_id", "instance_id", "seq", "id"),
    schema="runs",
)
"""`rng_uses` counts the instance's hook calls that drew from `ctx.rng`; with the run seed it
fixes the random stream the next call gets (migration 0007)."""

deliveries = Table(
    "deliveries",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("msg_seq", BigInteger, primary_key=True),
    Column("recipient", Text, primary_key=True),
    Column("status", Text, nullable=False),
    Column("delivered_seq", BigInteger),
    Column("due_turn", Integer),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{s}'" for s in DELIVERY_STATUSES) + ")", name="status"
    ),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    schema="runs",
)
"""One row per message and recipient. `pending` from the send; `dropped` (final) or `delayed`
with `due_turn`, the recipient's turn it is held for, when `before_deliver` says so; then
`delivered` with `delivered_seq`. A delayed message still `delayed` at run end was never
delivered (migration 0007)."""

checkpoints = Table(
    "checkpoints",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("turn", Integer, primary_key=True),
    Column("seq", BigInteger, nullable=False),
    Column("state", JSONB, nullable=False),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    Index(None, "run_id", "seq"),
    schema="runs",
)
"""The loop's state at the start of each run-wide turn, after the observers caught up: what a
fork goes on from. `seq` is the last event committed before the turn (migration 0008)."""

canaries = Table(
    "canaries",
    metadata,
    Column("run_id", Text, primary_key=True),
    Column("canaries", JSONB, nullable=False),
    Column("sandbox_canaries", JSONB, nullable=False),
    ForeignKeyConstraint(["run_id"], [control_runs.c.run_id]),
    schema="runs",
)
"""The run's canary tokens, written by the worker before the sandboxes exist, so a fork plants
the same ones (migration 0008)."""

judge_verdicts = Table(
    "judge_verdicts",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("question", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("from_seq", BigInteger),
    Column("to_seq", BigInteger),
    Column("status", Text, nullable=False),
    Column("answer", Text),
    Column("explanation", Text),
    Column("citations", JSONB, nullable=False),
    Column("rejection", Text),
    Column("request", JSONB, nullable=False),
    Column("response", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "status IN (" + ", ".join(f"'{s}'" for s in VERDICT_STATUSES) + ")", name="status"
    ),
    CheckConstraint(
        "answer IN (" + ", ".join(f"'{a}'" for a in VERDICT_ANSWERS) + ")", name="answer"
    ),
    Index(None, "run_id"),
    schema="analysis",
)
"""Owned by analysis: one row per judge call, accepted or not. No foreign key to `control.runs`:
analysis reads exports, not the orchestrator's tables, and its rows outlive a run's cleanup."""

rule_scans = Table(
    "rule_scans",
    metadata,
    Column("rule_set_sha256", Text, primary_key=True),
    Column("run_id", Text, primary_key=True),
    Column("rules", JSONB, nullable=False),
    Column("matches", Integer, nullable=False),
    Column("scanned_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index(None, "run_id"),
    schema="analysis",
)
"""Owned by analysis: one row per rule set and run scanned, matches or not, with the rule set
itself. Scanning again replaces the row and its matches."""

rule_matches = Table(
    "rule_matches",
    metadata,
    Column("rule_set_sha256", Text, primary_key=True),
    Column("run_id", Text, primary_key=True),
    Column("event_id", Text, primary_key=True),
    Column("rule_id", Text, primary_key=True),
    Column("seq", BigInteger, nullable=False),
    Column("field", Text, nullable=False),
    Column("via", JSONB, nullable=False),
    Column("excerpt", Text, nullable=False),
    ForeignKeyConstraint(
        ["rule_set_sha256", "run_id"], [rule_scans.c.rule_set_sha256, rule_scans.c.run_id]
    ),
    Index(None, "run_id", "event_id"),
    schema="analysis",
)
"""Owned by analysis: a rule's best match in one event, the one with the fewest decodings:
the payload field it was in, the decodings (`via`, outermost first), and an excerpt around it."""

analysis_jobs = Table(
    "jobs",
    metadata,
    Column("job_id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("actor", Text),
    Column("request", JSONB, nullable=False),
    Column("result", JSONB),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    CheckConstraint("status IN (" + ", ".join(f"'{s}'" for s in JOB_STATUSES) + ")", name="status"),
    schema="analysis",
)
"""Owned by analysis: background jobs of the analysis service, such as rule scans. A job runs in
the process that took it; one left `queued` or `running` by a process that stopped is marked
`failed` when the service next starts (migration 0011)."""
