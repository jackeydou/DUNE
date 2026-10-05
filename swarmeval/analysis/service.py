"""`swarmeval.analysis.v1.AnalysisService`: the analysis jobs as calls
(docs/services/analysis.md#interface)."""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

import grpc
import httpx2
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.analysis import compare, query, toolcalls, trace
from swarmeval.analysis.exports import ExportError, load_events, read_events_table, runs_of
from swarmeval.analysis.jobs import JobNotFound, JobRow, Jobs
from swarmeval.analysis.judge import Gateway, JudgeError, judge
from swarmeval.analysis.report import Rate, load_summaries, markdown, report
from swarmeval.analysis.scan import Match, scan_events, store_scan
from swarmeval.detect.rules import RuleSet, RuleSetError, parse_rules
from swarmeval.events import ObjectStore, events_key, export_key
from swarmeval.proto.swarmeval.analysis.v1 import analysis_pb2 as pb
from swarmeval.proto.swarmeval.analysis.v1.analysis_pb2_grpc import AnalysisServiceServicer

Context = grpc.aio.ServicerContext[Any, Any]

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
"""Run ids become object keys, so only the alphabet run ids are made of is accepted."""

DOWNLOAD_CHUNK_BYTES = 1 << 20
MAX_JOB_MATCHES = 500
"""Matches a rule scan job returns. All of them are in `analysis.rule_matches`."""

_log = logging.getLogger(__name__)


def _timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value)
    return stamp


def _rate(rate: Rate) -> pb.Rate:
    return pb.Rate(
        case_id=rate.case_id,
        case_sha256=rate.case_sha256,
        variant=rate.variant,
        variant_values_json=rate.task_args,
        scorer=rate.scorer,
        epochs=rate.epochs,
        rate=rate.rate,
        stderr=rate.stderr,
        ci_low=rate.ci_low,
        ci_high=rate.ci_high,
    )


def _match(match: Match) -> dict[str, JsonValue]:
    return {
        "run_id": match.run_id,
        "seq": match.seq,
        "event_id": match.event_id,
        "rule_id": match.rule_id,
        "field": match.field,
        "via": list(match.via),
        "excerpt": match.excerpt,
    }


def job_proto(job: JobRow) -> pb.Job:
    out = pb.Job(
        job_id=job.job_id,
        kind=job.kind,
        status=job.status,
        error=job.error or "",
        actor=job.actor or "",
        created_at=_timestamp(job.created_at),
        finished_at=_timestamp(job.finished_at),
    )
    if job.kind == "rule_scan" and job.result is not None:
        result: dict[str, Any] = job.result
        out.rule_scan.CopyFrom(
            pb.RuleScanResult(
                rule_set_sha256=result["rule_set_sha256"],
                runs=[pb.RuleScanRun(**run) for run in result["runs"]],
                matches=[pb.RuleMatch(**match) for match in result["matches"]],
                total_matches=result["total_matches"],
            )
        )
    return out


class AnalysisService(AnalysisServiceServicer):
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        store: ObjectStore,
        http: httpx2.AsyncClient,
        gateway: Gateway | None,
        query_timeout_s: float = query.TIMEOUT_S,
    ) -> None:
        """`gateway` is model-gateway with the analysis key, for `Judge`; without it `Judge`
        is refused. `http` is the client judge calls go out on."""
        self._engine = engine
        self._store = store
        self._http = http
        self._gateway = gateway
        self._query_timeout_s = query_timeout_s
        self._jobs = Jobs(engine)
        self._running: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Call once before serving."""
        left = await self._jobs.fail_unfinished()
        if left:
            _log.warning("%d analysis jobs were unfinished at start; marked failed", left)

    async def close(self) -> None:
        """Stops the jobs still running; they stay unfinished until the next start fails them."""
        for task in self._running:
            task.cancel()
        await asyncio.gather(*self._running, return_exceptions=True)

    async def _run_id(self, run_id: str, context: Context) -> str:
        if not RUN_ID.fullmatch(run_id) or ".." in run_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"{run_id!r} is not a run id. Copy one from the run list.",
            )
        return run_id

    async def Query(
        self, request: pb.QueryRequest, context: Context
    ) -> AsyncIterator[pb.QueryResponse]:
        if request.max_rows < 0 or request.max_rows > query.MAX_ROWS:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"max_rows is {request.max_rows}; give 1 to {query.MAX_ROWS}, or 0 for "
                f"{query.MAX_ROWS}. Aggregate or filter in the statement to see more.",
            )
        limit = request.max_rows or query.MAX_ROWS
        con = await asyncio.to_thread(query.connect, self._store)
        # `interrupt` is the one call DuckDB allows from another thread while a statement runs.
        timer = asyncio.get_running_loop().call_later(self._query_timeout_s, con.interrupt)
        try:
            columns, reader = await asyncio.to_thread(query.start, con, request.sql)
            yield pb.QueryResponse(
                columns=[pb.QueryColumn(name=name, type=kind) for name, kind in columns]
            )
            sent, more = 0, False
            while (rows := await asyncio.to_thread(query.fetch, reader)) is not None:
                if sent + len(rows) > limit:
                    rows, more = rows[: limit - sent], True
                sent += len(rows)
                chunk = pb.QueryResponse()
                for row in rows:
                    # The stubs type nested values as messages; at run time plain lists and
                    # dicts are taken.
                    chunk.rows.add().extend(row)  # pyright: ignore[reportArgumentType]
                if rows:
                    yield chunk
                if more:
                    break
                if sent == limit:
                    # Exactly at the limit: only another row makes the result cut short.
                    more = bool(await asyncio.to_thread(query.fetch, reader))
                    break
            yield pb.QueryResponse(truncated=more)
        except query.QueryError as err:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(err))
        except query.QueryTimeout:
            await context.abort(
                grpc.StatusCode.DEADLINE_EXCEEDED,
                f"the statement ran over {self._query_timeout_s:g} seconds and was stopped. "
                "Narrow it: filter `events` by `run_id`, or query `runs` first.",
            )
        finally:
            timer.cancel()
            con.close()

    async def SearchToolCalls(
        self, request: pb.SearchToolCallsRequest, context: Context
    ) -> pb.SearchToolCallsResponse:
        if request.limit < 0 or request.limit > toolcalls.MAX_LIMIT:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"limit is {request.limit}; give 1 to {toolcalls.MAX_LIMIT}, or 0 for "
                f"{toolcalls.DEFAULT_LIMIT}.",
            )
        run_ids = [await self._run_id(r, context) for r in request.run_ids]
        calls, truncated = await asyncio.to_thread(
            lambda: toolcalls.search(
                self._store,
                run_ids=run_ids,
                submission_ids=list(request.submission_ids),
                tool=request.tool,
                agent_id=request.agent_id,
                from_time=request.from_time.ToDatetime() if request.HasField("from_time") else None,
                to_time=request.to_time.ToDatetime() if request.HasField("to_time") else None,
                limit=request.limit or toolcalls.DEFAULT_LIMIT,
            )
        )
        return pb.SearchToolCallsResponse(
            calls=[
                pb.ToolCall(
                    run_id=c.run_id,
                    seq=c.seq,
                    event_id=c.event_id,
                    time=_timestamp(c.time),
                    agent_id=c.agent_id or "",
                    tool=c.tool,
                    arguments_json=c.arguments_json,
                    result=c.result,
                    error=c.error or "",
                )
                for c in calls
            ],
            truncated=truncated,
        )

    async def StartRuleScan(
        self, request: pb.StartRuleScanRequest, context: Context
    ) -> pb.StartRuleScanResponse:
        try:
            rules = parse_rules(request.rules_yaml, "the rule set")
        except RuleSetError as err:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(err))
        run_ids = {await self._run_id(r, context) for r in request.run_ids}
        if request.submission_ids:
            summaries = await asyncio.to_thread(load_summaries, self._store)
            try:
                run_ids.update(runs_of(summaries, request.submission_ids, ["done", "cancelled"]))
            except ExportError as err:
                await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
        if not run_ids:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "the scan names no runs. Give `run_ids`, `submission_ids`, or both.",
            )
        selected = sorted(run_ids)
        job = await self._jobs.create(
            "rule_scan",
            request.actor or None,
            {"rules": rules.model_dump(mode="json"), "run_ids": list(selected)},
        )
        task = asyncio.create_task(self._scan(job.job_id, rules, selected))
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return pb.StartRuleScanResponse(job=job_proto(job))

    async def _scan(self, job_id: str, rules: RuleSet, run_ids: Sequence[str]) -> None:
        await self._jobs.running(job_id)
        try:
            runs: list[JsonValue] = []
            first: list[JsonValue] = []
            total = 0
            for run_id in run_ids:
                rows = await load_events(self._store, run_id)
                matches = await asyncio.to_thread(scan_events, run_id, rows, rules)
                await store_scan(self._engine, run_id, rules, matches)
                runs.append({"run_id": run_id, "matches": len(matches)})
                first.extend(_match(m) for m in matches[: MAX_JOB_MATCHES - len(first)])
                total += len(matches)
        except ExportError as err:
            await self._jobs.failed(job_id, str(err))
            return
        except Exception as err:
            # The job is the caller's only view of the scan, so the failure goes on it; the
            # cause stays in the log.
            _log.error("rule scan job %s failed", job_id, exc_info=err)
            await self._jobs.failed(job_id, f"the scan failed: {type(err).__name__}: {err}")
            raise
        await self._jobs.done(
            job_id,
            {
                "rule_set_sha256": rules.sha256(),
                "runs": runs,
                "matches": first,
                "total_matches": total,
            },
        )

    async def GetJob(self, request: pb.GetJobRequest, context: Context) -> pb.GetJobResponse:
        try:
            return pb.GetJobResponse(job=job_proto(await self._jobs.get(request.job_id)))
        except JobNotFound as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))

    async def Judge(self, request: pb.JudgeRequest, context: Context) -> pb.JudgeResponse:
        if self._gateway is None:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "this deployment's analysis service has no judge key. Start it with "
                "SWARMEVAL_ANALYSIS_KEY set to the key model-gateway's `analysis_key_env` names.",
            )
        if not request.question.strip() or not request.model:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "a judge request needs a `question` and a `model`.",
            )
        run_id = await self._run_id(request.run_id, context)
        try:
            verdict = await judge(
                run_id,
                request.question,
                model=request.model,
                rows=await load_events(self._store, run_id),
                gateway=self._gateway,
                http=self._http,
                engine=self._engine,
                from_seq=request.from_seq or None,
                to_seq=request.to_seq or None,
            )
        except ExportError as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
        except JudgeError as err:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(err))
        return pb.JudgeResponse(
            status=verdict.status,
            answer=verdict.answer or "",
            explanation=verdict.explanation or "",
            citations=verdict.citations,
            rejection=verdict.rejection or "",
        )

    async def Report(self, request: pb.ReportRequest, context: Context) -> pb.ReportResponse:
        try:
            comparison = compare.parse_comparison(request.compare) if request.compare else None
        except compare.CompareError as err:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(err))
        summaries = await asyncio.to_thread(load_summaries, self._store)
        result = await asyncio.to_thread(
            report, summaries, list(request.submission_ids), list(request.suites)
        )
        text = markdown(result)
        differences: tuple[compare.Difference, ...] = ()
        if comparison is not None:
            differences = compare.compare(result, comparison)
            text += "\n" + compare.markdown(differences, comparison)
        return pb.ReportResponse(
            rates=[_rate(r) for r in result.rates],
            unscored=[
                pb.Unscored(
                    case_id=u.case_id,
                    case_sha256=u.case_sha256,
                    variant=u.variant,
                    variant_values_json=u.task_args,
                    status=u.status,
                    runs=u.runs,
                )
                for u in result.unscored
            ],
            coverage=[
                pb.Coverage(
                    case_id=c.case_id,
                    case_sha256=c.case_sha256,
                    variant=c.variant,
                    variant_values_json=c.task_args,
                    requested=c.requested,
                    done=c.done,
                    replaced=c.replaced,
                    missing=c.missing,
                )
                for c in result.coverage
            ],
            forks=[
                pb.ForkScore(
                    run_id=f.run_id,
                    forked_from=f.forked_from,
                    fork_seq=f.fork_seq,
                    fidelity=f.fidelity or "",
                    status=f.status,
                    scorer=f.scorer or "",
                    value=f.value,
                )
                for f in result.forks
            ],
            differences=[
                pb.Difference(
                    case_id=d.case_id,
                    case_sha256=d.case_sha256,
                    scorer=d.scorer,
                    others_json=d.others,
                    a=_rate(d.a),
                    b=_rate(d.b),
                    diff=d.diff,
                    ci_low=d.ci_low,
                    ci_high=d.ci_high,
                )
                for d in differences
            ],
            markdown=text,
        )

    async def GetTrace(self, request: pb.GetTraceRequest, context: Context) -> pb.GetTraceResponse:
        run_id = await self._run_id(request.run_id, context)

        def chain() -> trace.Trace:
            return trace.trace(
                read_events_table(self._store, run_id),
                request.event_id,
                event_chars=request.event_chars or 400,
                load=lambda other: read_events_table(self._store, other),
            )

        try:
            found = await asyncio.to_thread(chain)
        except (ExportError, trace.EventNotInRun) as err:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(err))
        except trace.TraceError as err:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(err))
        return pb.GetTraceResponse(
            links=[
                pb.TraceLink(
                    run_id=link.run_id,
                    seq=link.seq,
                    event_id=link.event_id,
                    agent_id=link.agent_id or "",
                    type=link.type,
                    line=link.text,
                )
                for link in found.links
            ]
        )

    async def DownloadExport(
        self, request: pb.DownloadExportRequest, context: Context
    ) -> AsyncIterator[pb.DownloadExportResponse]:
        run_id = await self._run_id(request.run_id, context)
        match request.format:
            case pb.EXPORT_FORMAT_EVAL:
                key = export_key(run_id)
            case pb.EXPORT_FORMAT_PARQUET:
                key = events_key(run_id)
            case _:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "name the file to download: format `EXPORT_FORMAT_EVAL` (the Inspect log) "
                    "or `EXPORT_FORMAT_PARQUET` (the events).",
                )
        try:
            source = await asyncio.to_thread(self._store.open, key)
        except FileNotFoundError:
            await context.abort(
                grpc.StatusCode.NOT_FOUND,
                f"run {run_id} has no `{key}` in the bucket. Only runs that ended `done` or "
                "`cancelled` are exported; check the run's status.",
            )
        # Read as it is sent: an export can be far larger than this process's memory.
        try:
            while chunk := await asyncio.to_thread(source.read, DOWNLOAD_CHUNK_BYTES):
                yield pb.DownloadExportResponse(chunk=chunk)
        finally:
            await asyncio.to_thread(source.close)
