"""`AnalysisService` over the shared Postgres and object store, with model-gateway mocked
(M4 spec decision 8)."""

import asyncio
import io
import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import grpc
import httpx2
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from google.protobuf.json_format import MessageToDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.analysis.judge import Gateway
from swarmeval.analysis.service import AnalysisService
from swarmeval.db import analysis_jobs, rule_matches
from swarmeval.events import (
    EVENTS_SCHEMA,
    SUMMARY_SCHEMA,
    ObjectStore,
    events_key,
    export_key,
    summary_key,
)
from swarmeval.proto.swarmeval.analysis.v1 import analysis_pb2 as pb
from swarmeval.proto.swarmeval.analysis.v1.analysis_pb2_grpc import (
    AnalysisServiceStub,
    add_AnalysisServiceServicer_to_server,
)
from tests.analysis.test_judge import verdict_args
from tests.analysis.test_report import summary
from tests.analysis.test_trace import CALL, DELIVER, SEND, START, linked
from tests.gateway.mock_backend import completion, tool_call

if TYPE_CHECKING:
    from swarmeval.proto.swarmeval.analysis.v1.analysis_pb2_grpc import AnalysisServiceAsyncStub

pytestmark = pytest.mark.docker

GATEWAY = Gateway(url="http://gateway", key="k" * 64)
SECRET = "zzINBOX"


def parquet(table: pa.Table) -> bytes:
    out = io.BytesIO()
    pq.write_table(table, out)  # pyright: ignore[reportUnknownMemberType]
    return out.getvalue()


@dataclass(frozen=True)
class Exported:
    """Two exported runs of one submission of the test's own, and one that was not exported."""

    submission: str
    runs: tuple[str, str]
    missing: str


@pytest.fixture
def exported(object_store: ObjectStore) -> Exported:
    submission = secrets.token_hex(4)
    runs = (f"probe.{submission}.v0.e1", f"probe.{submission}.v0.e2")
    for n, run_id in enumerate(runs):
        shell = {
            "event": "tool",
            "function": "shell",
            "arguments": {"cmd": f"echo {SECRET}" if n == 0 else "ls"},
            "result": SECRET if n == 0 else "a.txt",
        }
        rows = [
            linked(1, None, None, START),
            linked(2, 1, "a", {"event": "model", "output": {"choices": []}}),
            linked(3, 2, "a", CALL),
            linked(4, 2, "a", SEND),
            linked(5, 4, "b", DELIVER),
            linked(6, 5, "b", shell),
        ]
        for row in rows:
            row["run_id"] = run_id
            row["type"] = json.loads(row["payload"])["event"]
        object_store.put(
            events_key(run_id), parquet(pa.Table.from_pylist(rows, schema=EVENTS_SCHEMA))
        )
        object_store.put(export_key(run_id), b"eval-bytes-" + run_id.encode())
        row = summary(run_id, submission=submission, epoch=n + 1, epochs=2, scores={"leak": 1 - n})
        object_store.put(
            summary_key(run_id), parquet(pa.Table.from_pylist([row], schema=SUMMARY_SCHEMA))
        )
    return Exported(submission, runs, f"probe.{submission}.v0.e9")


def gateway_answering(arguments: str) -> httpx2.AsyncClient:
    async def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == f"Bearer {GATEWAY.key}"
        return httpx2.Response(
            200, json=completion("", tool_calls=[tool_call("verdict", arguments)])
        )

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


async def serving(service: AnalysisService) -> AsyncIterator["AnalysisServiceAsyncStub"]:
    await service.start()
    server = grpc.aio.server()
    add_AnalysisServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        yield AnalysisServiceStub(channel)
    await server.stop(None)
    await service.close()


@pytest.fixture
async def analysis(
    engine: AsyncEngine, object_store: ObjectStore
) -> AsyncIterator["AnalysisServiceAsyncStub"]:
    async with gateway_answering(verdict_args("yes", ["e6"])) as http:
        service = AnalysisService(engine=engine, store=object_store, http=http, gateway=GATEWAY)
        async for stub in serving(service):
            yield stub


async def query(
    analysis: "AnalysisServiceAsyncStub", sql: str, max_rows: int = 0
) -> tuple[list[str], list[list[Any]], bool]:
    columns: list[str] = []
    rows: list[list[Any]] = []
    truncated = False
    async for chunk in analysis.Query(pb.QueryRequest(sql=sql, max_rows=max_rows)):
        columns += [c.name for c in chunk.columns]
        rows += [list(MessageToDict(row)) for row in chunk.rows]
        truncated = truncated or chunk.truncated
    return columns, rows, truncated


async def refused(call: Any) -> tuple[grpc.StatusCode, str]:
    with pytest.raises(grpc.aio.AioRpcError) as info:
        result = call
        if hasattr(call, "__aiter__"):
            async for _ in call:
                pass
        else:
            await result
    return info.value.code(), str(info.value.details())


async def test_a_query_reads_the_runs_and_events_views(
    analysis: "AnalysisServiceAsyncStub", exported: Exported
) -> None:
    columns, rows, truncated = await query(
        analysis,
        "SELECT r.run_id, r.epoch, count(*) AS events, max(e.ts) AS last, "
        "9007199254740993 AS big "
        "FROM runs r JOIN events e USING (run_id) "
        f"WHERE r.submission_id = '{exported.submission}' GROUP BY ALL ORDER BY r.epoch",
    )

    assert columns == ["run_id", "epoch", "events", "last", "big"]
    assert [row[:3] for row in rows] == [[exported.runs[0], 1, 6], [exported.runs[1], 2, 6]]
    assert rows[0][3].startswith("20") and rows[0][4] == "9007199254740993"
    assert not truncated


async def test_a_query_is_cut_at_its_row_limit_and_says_so(
    analysis: "AnalysisServiceAsyncStub", exported: Exported
) -> None:
    sql = f"SELECT seq FROM events WHERE run_id = '{exported.runs[0]}' ORDER BY seq"

    _, cut, truncated = await query(analysis, sql, max_rows=4)
    _, exact, exact_truncated = await query(analysis, sql, max_rows=6)

    assert (cut, truncated) == ([[1], [2], [3], [4]], True)
    assert (len(exact), exact_truncated) == (6, False)
    code, _ = await refused(analysis.Query(pb.QueryRequest(sql=sql, max_rows=10_001)))
    assert code == grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM '/etc/passwd'",
        "SELECT * FROM read_text('/etc/hostname')",
        "SELECT * FROM read_parquet('s3://exports/summaries/*.parquet')",
        "SELECT * FROM glob('/*')",
        "SET enable_external_access = true",
        "SELECT 1; SET lock_configuration = false",
        "RESET enable_external_access",
        "COPY (SELECT 1) TO '/tmp/swarmeval-query-escape.csv'",
        "CREATE TABLE t AS SELECT 1",
        "INSTALL httpfs",
        "LOAD httpfs",
        "ATTACH '/tmp/swarmeval-query-escape.db' AS other",
        "DROP VIEW events",
        "PRAGMA enable_external_access = true",
        "SELECT * FROM pb",
        "SELECT * FROM not_a_view",
        "SELEC 1",
        "",
    ],
)
async def test_a_query_reads_nothing_else_and_changes_nothing(
    analysis: "AnalysisServiceAsyncStub", sql: str
) -> None:
    code, details = await refused(analysis.Query(pb.QueryRequest(sql=sql)))

    assert code == grpc.StatusCode.INVALID_ARGUMENT, details
    assert not Path("/tmp/swarmeval-query-escape.csv").exists()
    assert not Path("/tmp/swarmeval-query-escape.db").exists()
    # The connection after a refused statement is a new one, locked again.
    _, rows, _ = await query(
        analysis, "SELECT value FROM duckdb_settings() WHERE name = 'enable_external_access'"
    )
    assert rows == [["false"]]


async def test_a_query_over_its_time_limit_is_stopped(
    engine: AsyncEngine, object_store: ObjectStore
) -> None:
    async with httpx2.AsyncClient() as http:
        service = AnalysisService(
            engine=engine, store=object_store, http=http, gateway=None, query_timeout_s=0.3
        )
        code, details, rows = grpc.StatusCode.OK, "", list[list[Any]]()
        async for analysis in serving(service):
            code, details = await refused(
                analysis.Query(pb.QueryRequest(sql="SELECT count(*) FROM range(100000000000) t(i)"))
            )
            _, rows, _ = await query(analysis, "SELECT 1")

    assert code == grpc.StatusCode.DEADLINE_EXCEEDED and "0.3 seconds" in details
    assert rows == [[1]]


async def test_tool_calls_are_searched_by_run_tool_and_agent(
    analysis: "AnalysisServiceAsyncStub", exported: Exported
) -> None:
    by_submission = await analysis.SearchToolCalls(
        pb.SearchToolCallsRequest(submission_ids=[exported.submission])
    )
    shells = await analysis.SearchToolCalls(
        pb.SearchToolCallsRequest(run_ids=[exported.runs[0], exported.missing], tool="shell")
    )
    by_agent = await analysis.SearchToolCalls(
        pb.SearchToolCallsRequest(submission_ids=[exported.submission], agent_id="a", limit=1)
    )

    assert [(c.run_id, c.seq, c.tool) for c in by_submission.calls] == [
        (exported.runs[0], 3, "send_message"),
        (exported.runs[0], 6, "shell"),
        (exported.runs[1], 3, "send_message"),
        (exported.runs[1], 6, "shell"),
    ]
    (shell,) = shells.calls
    assert (shell.agent_id, shell.result, shell.event_id) == ("b", SECRET, "e6")
    assert json.loads(shell.arguments_json) == {"cmd": f"echo {SECRET}"}
    assert shell.HasField("time") and not shells.truncated
    assert [(c.run_id, c.seq) for c in by_agent.calls] == [(exported.runs[0], 3)]
    assert by_agent.truncated


async def finished(analysis: "AnalysisServiceAsyncStub", job_id: str) -> pb.Job:
    for _ in range(200):
        job = (await analysis.GetJob(pb.GetJobRequest(job_id=job_id))).job
        if job.status in ("done", "failed"):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


RULES = f"schema_version: 1\nrules:\n  - id: mailbox\n    keyword: {SECRET}\n"


async def test_a_rule_scan_runs_as_a_job_and_stores_its_matches(
    analysis: "AnalysisServiceAsyncStub", exported: Exported, engine: AsyncEngine
) -> None:
    started = await analysis.StartRuleScan(
        pb.StartRuleScanRequest(rules_yaml=RULES, submission_ids=[exported.submission], actor="ada")
    )

    assert (started.job.status, started.job.kind, started.job.actor) == (
        "queued",
        "rule_scan",
        "ada",
    )
    job = await finished(analysis, started.job.job_id)
    assert job.status == "done" and job.HasField("finished_at")
    scan = job.rule_scan
    assert [(r.run_id, r.matches) for r in scan.runs] == [
        (exported.runs[0], 1),
        (exported.runs[1], 0),
    ]
    assert scan.total_matches == 1
    (match,) = scan.matches
    assert (match.run_id, match.event_id, match.rule_id) == (exported.runs[0], "e6", "mailbox")
    assert SECRET in match.excerpt
    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                select(rule_matches.c.event_id).where(
                    rule_matches.c.rule_set_sha256 == scan.rule_set_sha256,
                    rule_matches.c.run_id == exported.runs[0],
                )
            )
        ).scalars()
        assert list(stored) == ["e6"]


async def test_a_rule_scan_that_cannot_run_is_refused_or_fails_its_job(
    analysis: "AnalysisServiceAsyncStub", exported: Exported
) -> None:
    code, details = await refused(
        analysis.StartRuleScan(
            pb.StartRuleScanRequest(rules_yaml="rules: []", run_ids=[exported.runs[0]])
        )
    )
    assert code == grpc.StatusCode.INVALID_ARGUMENT and "rule set" in details
    code, _ = await refused(analysis.StartRuleScan(pb.StartRuleScanRequest(rules_yaml=RULES)))
    assert code == grpc.StatusCode.INVALID_ARGUMENT
    code, _ = await refused(
        analysis.StartRuleScan(pb.StartRuleScanRequest(rules_yaml=RULES, submission_ids=["none"]))
    )
    assert code == grpc.StatusCode.NOT_FOUND
    code, _ = await refused(
        analysis.StartRuleScan(pb.StartRuleScanRequest(rules_yaml=RULES, run_ids=["../x"]))
    )
    assert code == grpc.StatusCode.INVALID_ARGUMENT
    code, _ = await refused(analysis.GetJob(pb.GetJobRequest(job_id="nope")))
    assert code == grpc.StatusCode.NOT_FOUND

    started = await analysis.StartRuleScan(
        pb.StartRuleScanRequest(rules_yaml=RULES, run_ids=[exported.missing])
    )
    job = await finished(analysis, started.job.job_id)
    assert job.status == "failed" and exported.missing in job.error


async def test_jobs_left_unfinished_by_a_stopped_service_are_failed_at_start(
    engine: AsyncEngine, object_store: ObjectStore
) -> None:
    job_id = secrets.token_hex(8)
    async with engine.begin() as conn:
        await conn.execute(
            analysis_jobs.insert().values(
                job_id=job_id, kind="rule_scan", status="running", request={}
            )
        )
    async with httpx2.AsyncClient() as http:
        service = AnalysisService(engine=engine, store=object_store, http=http, gateway=None)
        job = pb.Job()
        async for analysis in serving(service):
            job = (await analysis.GetJob(pb.GetJobRequest(job_id=job_id))).job

    assert job.status == "failed" and "restarted" in job.error


async def test_the_judge_answers_with_citations_or_is_refused(
    analysis: "AnalysisServiceAsyncStub",
    exported: Exported,
    engine: AsyncEngine,
    object_store: ObjectStore,
) -> None:
    verdict = await analysis.Judge(
        pb.JudgeRequest(run_id=exported.runs[0], question="Did b print it?", model="qwen-test")
    )

    assert (verdict.status, verdict.answer, list(verdict.citations)) == ("accepted", "yes", ["e6"])
    narrowed = await analysis.Judge(
        pb.JudgeRequest(run_id=exported.runs[0], question="q", model="m", to_seq=3)
    )
    assert narrowed.status == "rejected" and "e6" in narrowed.rejection
    code, _ = await refused(
        analysis.Judge(pb.JudgeRequest(run_id=exported.missing, question="q", model="m"))
    )
    assert code == grpc.StatusCode.NOT_FOUND
    code, _ = await refused(analysis.Judge(pb.JudgeRequest(run_id=exported.runs[0], model="m")))
    assert code == grpc.StatusCode.INVALID_ARGUMENT
    async with httpx2.AsyncClient() as http:
        keyless = AnalysisService(engine=engine, store=object_store, http=http, gateway=None)
        details = ""
        async for stub in serving(keyless):
            code, details = await refused(
                stub.Judge(pb.JudgeRequest(run_id=exported.runs[0], question="q", model="m"))
            )
    assert code == grpc.StatusCode.FAILED_PRECONDITION and "SWARMEVAL_ANALYSIS_KEY" in details


async def test_a_report_gives_rates_coverage_and_markdown(
    analysis: "AnalysisServiceAsyncStub", exported: Exported
) -> None:
    result = await analysis.Report(pb.ReportRequest(submission_ids=[exported.submission]))

    (rate,) = result.rates
    assert (rate.scorer, rate.epochs, rate.rate) == ("leak", 2, 0.5)
    assert rate.HasField("stderr") and 0 < rate.ci_low < 0.5 < rate.ci_high < 1
    assert json.loads(rate.variant_values_json) == {"framing": "v0"}
    (coverage,) = result.coverage
    assert (coverage.requested, coverage.done, coverage.missing) == (2, 2, 0)
    assert "| leak |" in result.markdown.replace("`", "")
    code, details = await refused(
        analysis.Report(pb.ReportRequest(submission_ids=[exported.submission], compare="framing"))
    )
    assert code == grpc.StatusCode.INVALID_ARGUMENT and "AXIS=A,B" in details
    compared = await analysis.Report(
        pb.ReportRequest(submission_ids=[exported.submission], compare="framing=v0,v1")
    )
    assert list(compared.differences) == [] and len(compared.markdown) > len(result.markdown)


async def test_a_trace_is_the_chain_from_the_root(
    analysis: "AnalysisServiceAsyncStub", exported: Exported
) -> None:
    traced = await analysis.GetTrace(pb.GetTraceRequest(run_id=exported.runs[0], event_id="e6"))

    assert [(link.seq, link.agent_id) for link in traced.links] == [
        (1, ""),
        (2, "a"),
        (4, "a"),
        (5, "b"),
        (6, "b"),
    ]
    assert {link.run_id for link in traced.links} == {exported.runs[0]}
    assert "shell" in traced.links[-1].line
    code, _ = await refused(
        analysis.GetTrace(pb.GetTraceRequest(run_id=exported.runs[0], event_id="e99"))
    )
    assert code == grpc.StatusCode.NOT_FOUND
    code, _ = await refused(
        analysis.GetTrace(pb.GetTraceRequest(run_id=exported.missing, event_id="e1"))
    )
    assert code == grpc.StatusCode.NOT_FOUND


async def test_exports_are_downloaded_in_chunks(
    analysis: "AnalysisServiceAsyncStub",
    exported: Exported,
    object_store: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def download(run_id: str, fmt: "pb.ExportFormat.ValueType") -> bytes:
        request = pb.DownloadExportRequest(run_id=run_id, format=fmt)
        return b"".join([part.chunk async for part in analysis.DownloadExport(request)])

    run_id = exported.runs[0]
    assert await download(run_id, pb.EXPORT_FORMAT_EVAL) == b"eval-bytes-" + run_id.encode()
    big = f"big.{exported.submission}.v0.e1"
    content = secrets.token_bytes((2 << 20) + 17)
    object_store.put(export_key(big), content)
    expected = object_store.get(events_key(run_id))

    # From here on a download that reads a whole object into memory fails: the service must
    # stream it.
    def whole_object(self: ObjectStore, key: str) -> bytes:
        raise AssertionError(f"{key} was read whole")

    monkeypatch.setattr(ObjectStore, "get", whole_object)
    request = pb.DownloadExportRequest(run_id=big, format=pb.EXPORT_FORMAT_EVAL)
    chunks = [part.chunk async for part in analysis.DownloadExport(request)]
    assert await download(run_id, pb.EXPORT_FORMAT_PARQUET) == expected
    assert [len(c) for c in chunks] == [1 << 20, 1 << 20, 17] and b"".join(chunks) == content

    for request in (
        pb.DownloadExportRequest(run_id=exported.missing, format=pb.EXPORT_FORMAT_EVAL),
        pb.DownloadExportRequest(run_id=run_id),
        pb.DownloadExportRequest(run_id="../summaries/x", format=pb.EXPORT_FORMAT_EVAL),
    ):
        code, _ = await refused(analysis.DownloadExport(request))
        assert code in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.INVALID_ARGUMENT)
