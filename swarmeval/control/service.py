"""`swarmeval.control.v1.ControlService` (docs/services/orchestrator.md#control-api)."""

import asyncio
import contextlib
import json
import secrets
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import grpc
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.bundles import bundle_hash, bundle_key, unpack
from swarmeval.control.live import EventListener
from swarmeval.control.queue import FINISHED, NewRun, Queue, RunFinished, RunNotFound, RunRow
from swarmeval.core import CaseError, LoadedCase, load_case
from swarmeval.core.models import Scalar
from swarmeval.db import events
from swarmeval.events import ObjectStore
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import ControlServiceServicer

Context = grpc.aio.ServicerContext[Any, Any]

POLL_S = 5.0
"""How long a live stream waits for a wakeup before it reads again anyway."""


def _struct(values: Mapping[str, JsonValue]) -> Struct:
    struct = Struct()
    struct.update(values)
    return struct


def _timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value)
    return stamp


def to_proto(run: RunRow) -> pb.Run:
    return pb.Run(
        run_id=run.run_id,
        submission_id=run.submission_id,
        case_id=run.case_id,
        workspace=run.workspace,
        status=run.status,
        variant=run.variant,
        task_args=_struct(run.task_args),
        epoch=run.epoch,
        epochs=run.epochs,
        case_sha256=run.case_sha256,
        owner_id=run.owner_id or "",
        isolation=run.isolation or "",
        error=run.error or "",
        created_at=_timestamp(run.created_at),
        started_at=_timestamp(run.started_at),
        finished_at=_timestamp(run.finished_at),
    )


def overrides_of(struct: Struct) -> dict[str, list[Scalar]]:
    """Struct numbers are doubles; a whole number comes back as an int, so `3` stays `3`."""
    raw: dict[str, object] = MessageToDict(struct)
    found: dict[str, list[Scalar]] = {}
    for axis, values in raw.items():
        if not isinstance(values, list):
            raise CaseError(f"override `{axis}` must be a list of values, got {values!r}.")
        found[axis] = []
        items: list[object] = values  # pyright: ignore[reportUnknownVariableType]
        for value in items:
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if not isinstance(value, str | int | float | bool):
                raise CaseError(f"override `{axis}` holds {value!r}; values must be scalars.")
            found[axis].append(value)
    return found


def plan_runs(
    loaded: LoadedCase,
    *,
    submission_id: str,
    case_sha256: str,
    overrides: Mapping[str, Sequence[Scalar]],
    epochs: int,
) -> list[NewRun]:
    return [
        NewRun(
            run_id=f"{loaded.id}.{submission_id}.v{variant.index}.e{epoch}",
            submission_id=submission_id,
            case_id=loaded.id,
            workspace=loaded.workspace,
            case_sha256=case_sha256,
            overrides={k: list(v) for k, v in overrides.items()},
            variant=variant.index,
            task_args=dict(variant.values),
            epoch=epoch,
            epochs=epochs,
        )
        for variant in loaded.variants
        for epoch in range(1, epochs + 1)
    ]


def _load(bundle: bytes, overrides: Mapping[str, Sequence[Scalar]]) -> LoadedCase:
    with tempfile.TemporaryDirectory(prefix="swarmeval-case-") as scratch:
        return load_case(unpack(bundle, Path(scratch)), overrides)


class ControlService(ControlServiceServicer):
    def __init__(
        self, *, queue: Queue, engine: AsyncEngine, store: ObjectStore, listener: EventListener
    ) -> None:
        self._queue = queue
        self._engine = engine
        self._store = store
        self._listener = listener

    async def SubmitRuns(
        self, request: pb.SubmitRunsRequest, context: Context
    ) -> pb.SubmitRunsResponse:
        if request.epochs < 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "epochs must not be negative.")
        try:
            overrides = overrides_of(request.overrides)
            loaded = await asyncio.to_thread(_load, request.case_bundle, overrides)
        except CaseError as err:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(err))
        sha256 = bundle_hash(request.case_bundle)
        await asyncio.to_thread(self._store.put, bundle_key(sha256), request.case_bundle)
        submission_id = secrets.token_hex(4)
        runs = plan_runs(
            loaded,
            submission_id=submission_id,
            case_sha256=sha256,
            overrides=overrides,
            epochs=request.epochs or loaded.epochs,
        )
        await self._queue.enqueue(runs)
        return pb.SubmitRunsResponse(submission_id=submission_id, run_ids=[r.run_id for r in runs])

    async def GetRun(self, request: pb.GetRunRequest, context: Context) -> pb.GetRunResponse:
        try:
            return pb.GetRunResponse(run=to_proto(await self._queue.get(request.run_id)))
        except RunNotFound as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))

    async def ListRuns(self, request: pb.ListRunsRequest, context: Context) -> pb.ListRunsResponse:
        runs = await self._queue.list_runs(
            submission_id=request.submission_id or None,
            case_id=request.case_id or None,
            status=request.status or None,
            limit=request.limit or 100,
        )
        return pb.ListRunsResponse(runs=[to_proto(r) for r in runs])

    async def CancelRun(
        self, request: pb.CancelRunRequest, context: Context
    ) -> pb.CancelRunResponse:
        try:
            return pb.CancelRunResponse(run=to_proto(await self._queue.cancel(request.run_id)))
        except RunNotFound as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
        except RunFinished as err:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(err))

    async def StreamEvents(
        self, request: pb.StreamEventsRequest, context: Context
    ) -> AsyncIterator[pb.StreamEventsResponse]:
        run_id, last = request.run_id, request.after_seq
        try:
            await self._queue.status(run_id)
        except RunNotFound as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
        with self._listener.subscribe(run_id) as wakeup:
            finished = False
            while True:
                wakeup.clear()
                batch = await self._read(run_id, last)
                for item in batch:
                    yield item
                    last = item.seq
                if batch:
                    continue
                if finished:
                    return
                # Read once more after seeing the run finished: its last commit may have landed
                # between the read above and the status check.
                finished = await self._queue.status(run_id) in FINISHED
                if not finished:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(wakeup.wait(), POLL_S)

    async def _read(self, run_id: str, after: int) -> list[pb.StreamEventsResponse]:
        query = (
            select(
                events.c.seq, events.c.event_id, events.c.type, events.c.agent_id, events.c.payload
            )
            .where(events.c.run_id == run_id, events.c.seq > after)
            .order_by(events.c.seq)
            .limit(500)
        )
        async with self._engine.connect() as conn:
            rows = await conn.execute(query)
            return [
                pb.StreamEventsResponse(
                    seq=seq,
                    event_id=event_id,
                    type=type_,
                    agent_id=agent_id or "",
                    payload_json=json.dumps(payload),
                )
                for seq, event_id, type_, agent_id, payload in rows
            ]
