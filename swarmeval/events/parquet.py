"""Parquet exports of a run: its events, and a one-row summary that reports read instead of whole
logs (docs/event-log.md#export). Derived from the rows, like the `.eval`.
"""

import asyncio
import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
from inspect_ai.event import Event, ScoreEvent
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.db import control_runs, events, run_specs
from swarmeval.events.export import ObjectStore

_EVENT = TypeAdapter[Event](Event)

EVENTS_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("seq", pa.int64()),
        ("event_id", pa.string()),
        ("ts", pa.timestamp("us", tz="UTC")),
        ("type", pa.string()),
        ("source", pa.string()),
        ("agent_id", pa.string()),
        ("sandbox_id", pa.string()),
        ("parent_id", pa.string()),
        ("prev_hash", pa.string()),
        ("hash", pa.string()),
        ("payload", pa.string()),
    ]
)
"""Hashes are hex; `payload` is the stored Inspect event as JSON text."""

SCORE = pa.struct([("scorer", pa.string()), ("value", pa.float64()), ("explanation", pa.string())])

SUMMARY_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("submission_id", pa.string()),
        ("case_id", pa.string()),
        ("case_sha256", pa.string()),
        ("workspace", pa.string()),
        ("variant", pa.int32()),
        ("task_args", pa.string()),
        ("epoch", pa.int32()),
        ("epochs", pa.int32()),
        ("status", pa.string()),
        ("error", pa.string()),
        ("isolation", pa.string()),
        ("started_at", pa.timestamp("us", tz="UTC")),
        ("finished_at", pa.timestamp("us", tz="UTC")),
        ("scores", pa.list_(SCORE)),
    ]
)
"""`task_args` is the variant's axis values as JSON text with sorted keys, so equal variants
compare equal. `scores` holds each scorer's last score."""


def events_key(run_id: str) -> str:
    return f"runs/{run_id}/events.parquet"


def summary_key(run_id: str) -> str:
    return f"summaries/{run_id}.parquet"


async def export_events(engine: AsyncEngine, run_id: str, store: ObjectStore) -> str:
    """Writes the run's events as Parquet and returns the key. Export after the chain verified
    (`export_run`): this copies the rows as they are."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    events.c.run_id,
                    events.c.seq,
                    events.c.event_id,
                    events.c.ts,
                    events.c.type,
                    events.c.source,
                    events.c.agent_id,
                    events.c.sandbox_id,
                    events.c.parent_id,
                    events.c.prev_hash,
                    events.c.hash,
                    events.c.payload,
                )
                .where(events.c.run_id == run_id)
                .order_by(events.c.seq)
            )
        ).mappings()
    table = pa.Table.from_pylist(
        [
            {
                **r,
                "prev_hash": bytes(r["prev_hash"]).hex(),
                "hash": bytes(r["hash"]).hex(),
                "payload": json.dumps(r["payload"], ensure_ascii=False),
            }
            for r in rows
        ],
        schema=EVENTS_SCHEMA,
    )
    key = events_key(run_id)
    await asyncio.to_thread(store.put, key, _parquet(table))
    return key


async def export_summary(engine: AsyncEngine, run_id: str, store: ObjectStore) -> str:
    """Writes the run's summary and returns the key. Call it once `control.runs` holds the run's
    final status, which the summary copies: a cancel that lands while a worker finishes is
    whatever `control.runs` kept."""
    async with engine.connect() as conn:
        spec = (
            (
                await conn.execute(
                    select(
                        run_specs.c.submission_id,
                        run_specs.c.case_id,
                        run_specs.c.case_sha256,
                        run_specs.c.variant,
                        run_specs.c.task_args,
                        run_specs.c.epoch,
                        run_specs.c.epochs,
                        control_runs.c.workspace,
                        control_runs.c.isolation,
                        control_runs.c.started_at,
                        control_runs.c.finished_at,
                        control_runs.c.status,
                        control_runs.c.error,
                    )
                    .join(control_runs, control_runs.c.run_id == run_specs.c.run_id)
                    .where(run_specs.c.run_id == run_id)
                )
            )
            .mappings()
            .one()
        )
        payloads = (
            await conn.execute(
                select(events.c.payload)
                .where(events.c.run_id == run_id, events.c.type == "score")
                .order_by(events.c.seq)
            )
        ).scalars()
        scores: dict[str, dict[str, object]] = {}
        for payload in payloads:
            event = _EVENT.validate_python(payload)
            if isinstance(event, ScoreEvent) and event.scorer:
                scores[event.scorer] = {
                    "scorer": event.scorer,
                    "value": event.score.as_float(),
                    "explanation": event.score.explanation,
                }
    row = {
        **spec,
        "run_id": run_id,
        "task_args": json.dumps(spec["task_args"], sort_keys=True, ensure_ascii=False),
        "scores": list(scores.values()),
    }
    table = pa.Table.from_pylist([row], schema=SUMMARY_SCHEMA)
    key = summary_key(run_id)
    await asyncio.to_thread(store.put, key, _parquet(table))
    return key


def _parquet(table: pa.Table) -> bytes:
    out = io.BytesIO()
    pq.write_table(table, out)  # pyright: ignore[reportUnknownMemberType]
    return out.getvalue()
