"""`swarmeval.control.v1.ControlService` (docs/services/orchestrator.md#control-api)."""

import asyncio
import contextlib
import json
import re
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
from pydantic import JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.bundles import bundle_hash, bundle_key, unpack
from swarmeval.control.live import EventListener
from swarmeval.control.queue import (
    FINISHED,
    NewRun,
    Queue,
    RunFinished,
    RunNotFound,
    RunNotPaused,
    RunRow,
    run_id_of,
)
from swarmeval.core import (
    CaseError,
    LoadedCase,
    LoadedSuite,
    SuiteError,
    load_case,
    load_suite_text,
)
from swarmeval.core.models import AxisValue, Scalar, axis_json
from swarmeval.db import events
from swarmeval.events import ObjectStore, export_summary
from swarmeval.events.fork import ForkEventNotFound, ForkPointError, fork_point
from swarmeval.events.render import event_line
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import ControlServiceServicer
from swarmeval.runtime.fork import (
    DeleteMessage,
    Edit,
    ForkError,
    ReplaceDelivery,
    ReplaceMessage,
    check_edits,
)

Context = grpc.aio.ServicerContext[Any, Any]

LINE_CHARS = 2_000
"""Where a streamed event's `line` is cut, as the judge cuts it."""

POLL_S = 5.0
"""How long a live stream waits for a wakeup before it reads again anyway."""

SUITE_LABEL = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


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
        replaces=run.replaces or "",
        suite=run.suite or "",
        forked_from=run.forked_from or "",
        fork_seq=run.fork_seq or 0,
        fidelity=run.fidelity or "",
        submitted_by=run.submitted_by or "",
        cancelled_by=run.cancelled_by or "",
        resumed_by=run.resumed_by or "",
    )


def overrides_of(struct: Struct) -> dict[str, list[AxisValue]]:
    """Struct numbers are doubles; a whole number comes back as an int, so `3` stays `3`. A
    value may be a list of scalars, as a case's axis values may."""
    raw: dict[str, object] = MessageToDict(struct)
    found: dict[str, list[AxisValue]] = {}
    for axis, values in raw.items():
        if not isinstance(values, list):
            raise CaseError(f"override `{axis}` must be a list of values, got {values!r}.")
        items: list[object] = values  # pyright: ignore[reportUnknownVariableType]
        found[axis] = []
        for value in items:
            if isinstance(value, list):
                inner: list[object] = value  # pyright: ignore[reportUnknownVariableType]
                found[axis].append(tuple(_scalar(axis, v) for v in inner))
            else:
                found[axis].append(_scalar(axis, value))
    return found


def _scalar(axis: str, value: object) -> Scalar:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, str | int | float | bool):
        raise CaseError(
            f"override `{axis}` holds {value!r}; values must be scalars or lists of scalars."
        )
    return value


def edit_of(edit: pb.ForkEdit) -> Edit:
    match edit.WhichOneof("edit"):
        case "replace_message":
            m = edit.replace_message
            return ReplaceMessage(agent_id=m.agent_id, index=m.index, content=m.content)
        case "delete_message":
            return DeleteMessage(
                agent_id=edit.delete_message.agent_id, index=edit.delete_message.index
            )
        case "replace_delivery":
            d = edit.replace_delivery
            return ReplaceDelivery(
                send_event_id=d.send_event_id, recipient=d.recipient, content=d.content
            )
        case _:
            raise ForkError("a fork edit names no edit; set one of its fields.")


def plan_runs(
    loaded: LoadedCase,
    *,
    submission_id: str,
    case_sha256: str,
    overrides: Mapping[str, Sequence[AxisValue]],
    epochs: int,
    suite: str | None = None,
    submitted_by: str | None = None,
) -> list[NewRun]:
    return [
        NewRun(
            run_id=run_id_of(loaded.id, submission_id, variant.index, epoch),
            submission_id=submission_id,
            case_id=loaded.id,
            workspace=loaded.workspace,
            case_sha256=case_sha256,
            overrides={k: [axis_json(x) for x in v] for k, v in overrides.items()},
            variant=variant.index,
            task_args={k: axis_json(v) for k, v in variant.values.items()},
            epoch=epoch,
            epochs=epochs,
            suite=suite,
            submitted_by=submitted_by,
        )
        for variant in loaded.variants
        for epoch in range(1, epochs + 1)
    ]


def _load(bundle: bytes, overrides: Mapping[str, Sequence[AxisValue]]) -> LoadedCase:
    with tempfile.TemporaryDirectory(prefix="swarmeval-case-") as scratch:
        return load_case(unpack(bundle, Path(scratch)), overrides)


def _load_suite(text: str, bundles: Mapping[str, bytes]) -> LoadedSuite:
    """Unpacks each bundle and loads the suite against them. A path the suite names with no
    bundle, or a bundle the suite does not name, is a `SuiteError`."""
    with tempfile.TemporaryDirectory(prefix="swarmeval-suite-") as scratch:
        dirs: dict[str, Path] = {}
        for i, (path, bundle) in enumerate(sorted(bundles.items())):
            into = Path(scratch) / f"case{i}"
            into.mkdir()
            try:
                dirs[path] = unpack(bundle, into)
            except CaseError as err:
                raise SuiteError(f"suite case `{path}`: {err}") from err

        def case_dir(path: str) -> Path:
            if path not in dirs:
                raise SuiteError(
                    f"the suite names case `{path}`, and no bundle was sent for it. Send each "
                    "`cases[].path` as written in the suite with its directory's bundle."
                )
            return dirs[path]

        loaded = load_suite_text(text, source="the suite", case_dir=case_dir)
    unused = sorted(set(bundles) - {e.path for e in loaded.entries})
    if unused:
        raise SuiteError(
            f"bundles were sent for {', '.join(f'`{u}`' for u in unused)}, which the suite does "
            "not name. Send only the cases its `cases[].path` lists."
        )
    return loaded


class ControlService(ControlServiceServicer):
    def __init__(
        self,
        *,
        queue: Queue,
        engine: AsyncEngine,
        store: ObjectStore,
        listener: EventListener,
        allow_case_code: bool = False,
    ) -> None:
        self._queue = queue
        self._engine = engine
        self._store = store
        self._listener = listener
        self._allow_case_code = allow_case_code

    async def SubmitRuns(
        self, request: pb.SubmitRunsRequest, context: Context
    ) -> pb.SubmitRunsResponse:
        if request.epochs < 0:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "epochs must not be negative.")
        if request.suite and not SUITE_LABEL.fullmatch(request.suite):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"suite label {request.suite!r} is not valid. Use up to 128 lowercase letters, "
                "digits, `_`, `.`, and `-`, starting with a letter or digit.",
            )
        try:
            overrides = overrides_of(request.overrides)
            loaded = await asyncio.to_thread(_load, request.case_bundle, overrides)
        except CaseError as err:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(err))
        await self._refuse_case_code(loaded, context)
        sha256 = bundle_hash(request.case_bundle)
        await asyncio.to_thread(self._store.put, bundle_key(sha256), request.case_bundle)
        submission_id = secrets.token_hex(4)
        runs = plan_runs(
            loaded,
            submission_id=submission_id,
            case_sha256=sha256,
            overrides=overrides,
            epochs=request.epochs or loaded.epochs,
            suite=request.suite or None,
            submitted_by=request.actor or None,
        )
        await self._queue.enqueue(runs)
        return pb.SubmitRunsResponse(submission_id=submission_id, run_ids=[r.run_id for r in runs])

    async def _refuse_case_code(self, loaded: LoadedCase, context: Context) -> None:
        code = sorted({use for v in loaded.variants for use in v.code})
        if code and not self._allow_case_code:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"case `{loaded.id}` loads extensions from its own directory "
                f"({', '.join(code)}), and this deployment does not run case code: it would run "
                "inside the workers with their privileges. Start the control plane and the "
                "workers with `--allow-case-code` to accept it.",
            )

    async def SubmitSuite(
        self, request: pb.SubmitSuiteRequest, context: Context
    ) -> pb.SubmitSuiteResponse:
        bundles = dict(request.case_bundles)
        try:
            loaded = await asyncio.to_thread(_load_suite, request.suite_yaml, bundles)
        except SuiteError as err:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(err))
        for entry in loaded.entries:
            await self._refuse_case_code(entry.case, context)
        label = f"{loaded.id}.{secrets.token_hex(4)}"
        hashes = {path: bundle_hash(bundle) for path, bundle in bundles.items()}
        for path, sha256 in hashes.items():
            await asyncio.to_thread(self._store.put, bundle_key(sha256), bundles[path])
        runs: list[NewRun] = []
        submissions: list[pb.SuiteSubmission] = []
        for entry in loaded.entries:
            submission_id = secrets.token_hex(4)
            planned = plan_runs(
                entry.case,
                submission_id=submission_id,
                case_sha256=hashes[entry.path],
                overrides=entry.overrides,
                epochs=entry.epochs or entry.case.epochs,
                suite=label,
                submitted_by=request.actor or None,
            )
            runs.extend(planned)
            submissions.append(
                pb.SuiteSubmission(
                    case_id=entry.case.id,
                    submission_id=submission_id,
                    run_ids=[r.run_id for r in planned],
                )
            )
        # One transaction for every case: the suite is queued whole or not at all.
        await self._queue.enqueue(runs)
        return pb.SubmitSuiteResponse(suite=label, submissions=submissions)

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
            suite=request.suite or None,
            limit=request.limit or 100,
        )
        return pb.ListRunsResponse(runs=[to_proto(r) for r in runs])

    async def CancelRun(
        self, request: pb.CancelRunRequest, context: Context
    ) -> pb.CancelRunResponse:
        try:
            run = await self._queue.cancel(request.run_id, request.actor or None)
        except RunNotFound as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
        except RunFinished as err:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(err))
        if run.finished_at is not None:
            # A queued run never reaches a worker, so its summary is written here. A running
            # one is finished, and summarized, by its worker.
            await export_summary(self._engine, run.run_id, self._store)
        return pb.CancelRunResponse(run=to_proto(run))

    async def ResumeRun(
        self, request: pb.ResumeRunRequest, context: Context
    ) -> pb.ResumeRunResponse:
        try:
            run = await self._queue.resume(request.run_id, request.actor or None)
        except RunNotFound as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
        except RunNotPaused as err:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(err))
        return pb.ResumeRunResponse(run=to_proto(run))

    async def ForkRun(self, request: pb.ForkRunRequest, context: Context) -> pb.ForkRunResponse:
        try:
            source = await self._queue.get(request.run_id)
        except RunNotFound as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
        if source.status not in ("done", "cancelled"):
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"run `{source.run_id}` is `{source.status}`. Only a run that ended `done` or "
                "`cancelled` can be forked: those are exported, so a fork's trace can follow "
                "its parents into the source.",
            )
        try:
            edits = [edit_of(e) for e in request.edits]
        except (ForkError, ValidationError) as err:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"fork edits: {err}")
        try:
            point = await fork_point(self._engine, source.run_id, request.at_event_id)
            check_edits(edits, point.contexts, point.checkpoint.mail)
        except ForkEventNotFound as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
        except ForkPointError as err:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(err))
        except ForkError as err:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(err))
        run = await self._queue.fork(
            source, point.seq, [e.model_dump(mode="json") for e in edits], request.actor or None
        )
        return pb.ForkRunResponse(run=to_proto(run))

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
                    line=event_line(payload, LINE_CHARS),
                )
                for seq, event_id, type_, agent_id, payload in rows
            ]
