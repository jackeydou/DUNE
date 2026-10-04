"""Run export: the stored rows of one run as a standard Inspect `.eval` (docs/event-log.md#export).

The rows are the evidence original; the export is derived from them. The chain is verified
before anything is written, so a log that exists was produced from rows that verified.
"""

import asyncio
import copy
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

from inspect_ai.event import (
    Event,
    InfoEvent,
    ModelEvent,
    SampleLimitEvent,
    ScoreEvent,
    SpanBeginEvent,
    SpanEndEvent,
)
from inspect_ai.log import (
    EvalConfig,
    EvalDataset,
    EvalError,
    EvalLog,
    EvalMetric,
    EvalResults,
    EvalSample,
    EvalSampleLimit,
    EvalScore,
    EvalSpec,
    write_eval_log,
)
from inspect_ai.model import ModelUsage
from inspect_ai.scorer import Score
from pyarrow.fs import S3FileSystem
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.db import control_runs, events, messages, run_specs
from swarmeval.events.chain import ChainRow, ChainStart, verify
from swarmeval.events.convert import SCHEMA_VERSION, to_inspect_message
from swarmeval.runtime.messages import ChatMessage
from swarmeval.runtime.records import LifecycleRecord

_EVENT = TypeAdapter[Event](Event)
_MESSAGE = TypeAdapter[ChatMessage](ChatMessage)
_LIFECYCLE = TypeAdapter[LifecycleRecord](LifecycleRecord)


@dataclass(frozen=True)
class RunHeader:
    """What the export needs that the rows do not hold: the run's place in its case."""

    run_id: str
    case_id: str
    variant: int
    task_args: Mapping[str, JsonValue]
    """The variant's axis values."""
    epoch: int
    epochs: int
    input: str
    """The task as the case states it; Inspect shows it as the sample's input."""
    models: Mapping[str, str]
    """Agent id to model, in the case's agent order. The first agent's model is `eval.model`."""
    deterministic: bool = True
    """`False` under the `async` turn policy: the order of events is recorded, not
    reproducible."""


@dataclass(frozen=True)
class StoredRun:
    workspace: str
    events: Sequence[ChainRow]
    messages: Mapping[tuple[str, int], Sequence[ChatMessage]]
    """`(agent_id, gen)` to that generation's messages, in `idx` order."""
    forked_from: str | None = None
    chain_start: ChainStart | None = None
    """For a fork: where its chain links into its source's."""


@dataclass(frozen=True)
class ObjectStore:
    """An S3-compatible bucket, reached through the standard S3 API only."""

    endpoint: str
    """`host:port`."""
    bucket: str
    access_key: str
    secret_key: str
    scheme: Literal["http", "https"] = "https"
    region: str = "us-east-1"

    def filesystem(self) -> S3FileSystem:
        return S3FileSystem(
            access_key=self.access_key,
            secret_key=self.secret_key,
            endpoint_override=self.endpoint,
            scheme=self.scheme,
            region=self.region,
        )

    def put(self, key: str, data: bytes) -> None:
        """Blocking; call it through `asyncio.to_thread`."""
        with self.filesystem().open_output_stream(f"{self.bucket}/{key}") as out:
            out.write(data)

    def get(self, key: str) -> bytes:
        """Blocking; call it through `asyncio.to_thread`."""
        with self.filesystem().open_input_stream(f"{self.bucket}/{key}") as src:
            return src.read()


def export_key(run_id: str) -> str:
    return f"runs/{run_id}/sample.eval"


async def load_run(engine: AsyncEngine, run_id: str) -> StoredRun:
    async with engine.connect() as conn:
        workspace = (
            await conn.execute(
                select(control_runs.c.workspace).where(control_runs.c.run_id == run_id)
            )
        ).scalar_one()
        rows = await conn.execute(
            select(events.c.seq, events.c.prev_hash, events.c.hash, events.c.payload)
            .where(events.c.run_id == run_id)
            .order_by(events.c.seq)
        )
        chain = [ChainRow(seq=s, prev_hash=p, hash=h, payload=j) for s, p, h, j in rows]
        stored = await conn.execute(
            select(messages.c.agent_id, messages.c.gen, messages.c.message)
            .where(messages.c.run_id == run_id)
            .order_by(messages.c.agent_id, messages.c.gen, messages.c.idx)
        )
        by_gen: dict[tuple[str, int], list[ChatMessage]] = defaultdict(list)
        for agent_id, gen, message in stored:
            by_gen[(agent_id, gen)].append(_MESSAGE.validate_python(message))
        spec = (
            await conn.execute(
                select(run_specs.c.forked_from, run_specs.c.fork_seq).where(
                    run_specs.c.run_id == run_id
                )
            )
        ).one_or_none()
        forked_from = spec.forked_from if spec is not None else None
        start: ChainStart | None = None
        if spec is not None and spec.forked_from is not None:
            fork_seq: int = spec.fork_seq
            digest = (
                await conn.execute(
                    select(events.c.hash).where(
                        events.c.run_id == forked_from, events.c.seq == fork_seq
                    )
                )
            ).scalar_one()
            start = ChainStart(seq=fork_seq, hash=bytes(digest))
    return StoredRun(
        workspace=workspace,
        events=chain,
        messages=by_gen,
        forked_from=forked_from,
        chain_start=start,
    )


def assemble(header: RunHeader, run: StoredRun) -> EvalLog:
    """Raises `ChainError` if the rows do not verify."""
    if not run.events:
        raise ValueError(
            f"run {header.run_id} has no events, so there is nothing to export. Export runs "
            "after the run has started."
        )
    verify(header.run_id, run.events, run.chain_start)
    stored = [_restore(row, run) for row in run.events]
    sample_events = _with_agent_spans(stored, tuple(header.models))
    first, last = stored[0].timestamp, stored[-1].timestamp
    status, error = _outcome(stored)
    scores = _scores(stored)
    sample = EvalSample(
        id=header.case_id,
        epoch=header.epoch,
        input=header.input,
        target="",
        events=sample_events,
        model_usage=_model_usage(stored),
        started_at=first.isoformat(),
        completed_at=last.isoformat(),
        total_time=(last - first).total_seconds(),
        uuid=header.run_id,
        error=error,
        limit=_limit(stored),
        scores=scores or None,
        metadata={
            "swarmeval": {
                "schema_version": SCHEMA_VERSION,
                "run_id": header.run_id,
                **(
                    {"forked_from": run.forked_from, "fork_seq": run.chain_start.seq}
                    if run.forked_from is not None and run.chain_start is not None
                    else {}
                ),
            }
        },
    )
    spec = EvalSpec(
        run_id=header.run_id,
        created=first.isoformat(),
        task=header.case_id,
        task_args=dict(header.task_args),
        dataset=EvalDataset(name=header.case_id, samples=1, sample_ids=[header.case_id]),
        model=next(iter(header.models.values())),
        config=EvalConfig(epochs=header.epochs),
        packages={"inspect_ai": version("inspect_ai"), "swarmeval": version("swarmeval")},
        metadata={
            "swarmeval": {
                "schema_version": SCHEMA_VERSION,
                "workspace": run.workspace,
                "variant": header.variant,
                "models": dict(header.models),
                "deterministic": header.deterministic,
            }
        },
    )
    results = EvalResults(
        total_samples=1,
        completed_samples=1 if status == "success" else 0,
        scores=[
            EvalScore(
                name=name,
                scorer=name,
                reducer="mean",
                scored_samples=1,
                metrics={"mean": EvalMetric(name="mean", value=float(score.as_float()))},
                metadata=score.metadata,
            )
            for name, score in scores.items()
        ],
    )
    return EvalLog(eval=spec, status=status, samples=[sample], error=error, results=results)


def _scores(stored: list[Event]) -> dict[str, Score]:
    """The last score each scorer wrote. One sample per log until M1 assembles epochs, so a
    scorer's mean is its one value."""
    return {e.scorer: e.score for e in stored if isinstance(e, ScoreEvent) and e.scorer}


def write_eval(log: EvalLog, path: Path) -> None:
    write_eval_log(log, path, format="eval")


async def export_run(engine: AsyncEngine, header: RunHeader, store: ObjectStore) -> str:
    """Writes the run's `.eval` to `store` and returns its key within the bucket."""
    log = assemble(header, await load_run(engine, header.run_id))
    key = export_key(header.run_id)
    await asyncio.to_thread(_upload, log, store, key)
    return key


def _upload(log: EvalLog, store: ObjectStore, key: str) -> None:
    with tempfile.TemporaryDirectory(prefix="swarmeval-export-") as scratch:
        path = Path(scratch) / "sample.eval"
        write_eval(log, path)
        store.put(key, path.read_bytes())


def _restore(row: ChainRow, run: StoredRun) -> Event:
    """The stored payload, with the chain fields added and `ModelEvent.input` expanded."""
    payload: dict[str, Any] = copy.deepcopy(row.payload)  # pyright: ignore[reportAssignmentType]
    ours = payload["metadata"]["swarmeval"]
    ours["prev_hash"] = row.prev_hash.hex()
    ours["hash"] = row.hash.hex()
    event = _EVENT.validate_python(payload)
    if isinstance(event, ModelEvent) and ours["agent_id"] is not None:
        ref = ours["input"]
        if ref["gen"] is not None:
            context = run.messages[(ours["agent_id"], ref["gen"])][: ref["len"]]
            event.input = [to_inspect_message(m) for m in context]
    return event


def _agent_of(event: Event) -> str | None:
    assert event.metadata is not None, "every stored event carries metadata.swarmeval"
    return event.metadata["swarmeval"]["agent_id"]


def _with_agent_spans(stored: list[Event], agents: tuple[str, ...]) -> list[Event]:
    """Each agent is a span of `type="agent"`: it opens before the agent's first event and
    closes after its last, and every event the agent caused names it as `span_id`."""
    last_index = {_agent_of(e): i for i, e in enumerate(stored)}
    opened: set[str] = set()
    out: list[Event] = []
    for i, event in enumerate(stored):
        agent_id = _agent_of(event)
        if agent_id is not None:
            span = f"agent:{agent_id}"
            if agent_id not in opened:
                opened.add(agent_id)
                begin = SpanBeginEvent(id=span, name=agent_id, type="agent")
                begin.timestamp = event.timestamp
                begin.working_start = event.working_start
                out.append(begin)
            event.span_id = span
        out.append(event)
        if agent_id is not None and last_index[agent_id] == i:
            end = SpanEndEvent(id=f"agent:{agent_id}")
            end.timestamp = event.timestamp
            end.working_start = event.working_start
            out.append(end)
    unknown = opened - set(agents)
    if unknown:
        raise ValueError(
            f"events name agents {sorted(unknown)} that the run header does not list "
            f"({', '.join(agents)}). Build the header from the same case variant as the run."
        )
    return out


def _model_usage(stored: list[Event]) -> dict[str, ModelUsage]:
    usage: dict[str, ModelUsage] = {}
    for event in stored:
        if isinstance(event, ModelEvent) and event.output.usage is not None:
            usage[event.model] = usage.get(event.model, ModelUsage()) + event.output.usage
    return usage


def _outcome(
    stored: list[Event],
) -> tuple[Literal["success", "error"], EvalError | None]:
    ends = [e for e in stored if isinstance(e, InfoEvent) and e.source == "swarmeval.lifecycle"]
    if not ends:
        return "success", None
    last = _LIFECYCLE.validate_python({"kind": "lifecycle", **_as_dict(ends[-1].data)})
    if last.status != "failed":
        return "success", None
    message = last.error or last.reason or "the run failed"
    return "error", EvalError(message=message, traceback="", traceback_ansi="")


def _as_dict(data: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(data, dict), "lifecycle data is a dumped LifecycleRecord"
    return data


def _limit(stored: list[Event]) -> EvalSampleLimit | None:
    hit = next((e for e in stored if isinstance(e, SampleLimitEvent)), None)
    if hit is None:
        return None
    return EvalSampleLimit(type=hit.type, limit=hit.limit or 0, reason=hit.message)
