"""edge in front of the real Control API: the built `edge` binary, a user created with
`edge user create`, a case submitted over the public API as the console and CLI will submit it
(Connect's JSON protocol), run by the worker, and its events streamed back through edge
(M4 spec, Plan step 2)."""

import asyncio
import base64
import json
import os
import re
import secrets
import struct
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import httpx2
import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.analysis.service import AnalysisService
from swarmeval.control.bundles import pack
from swarmeval.events import ObjectStore, events_key, export_key
from swarmeval.proto.swarmeval.analysis.v1.analysis_pb2_grpc import (
    add_AnalysisServiceServicer_to_server,
)
from tests.containers import free_port, go_build, wait_listening
from tests.gateway.mock_backend import completion, tool_call
from tests.worker.conftest import Platform
from tests.worker.test_end_to_end import DEV_CMD, write_case

pytestmark = pytest.mark.docker

PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="session")
def edge_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return go_build("edge", tmp_path_factory.mktemp("edge-bin"))


@dataclass
class Edge:
    url: str
    http: httpx2.AsyncClient

    async def call(self, method: str, body: dict[str, Any], **headers: str) -> httpx2.Response:
        return await self.http.post(
            f"{self.url}/swarmeval.api.v1.{method}",
            json=body,
            headers={"Content-Type": "application/json", **headers},
        )

    async def stream(self, method: str, body: dict[str, Any], **headers: str) -> list[Any]:
        """A server-streaming call: one enveloped request, enveloped responses until the
        end-of-stream envelope (flag 0x02), whose error, if any, is raised."""
        payload = json.dumps(body).encode()
        response = await self.http.post(
            f"{self.url}/swarmeval.api.v1.{method}",
            content=struct.pack(">BI", 0, len(payload)) + payload,
            headers={"Content-Type": "application/connect+json", **headers},
        )
        assert response.status_code == 200, response.text
        data, messages = response.content, list[Any]()
        while data:
            flags, size = struct.unpack(">BI", data[:5])
            message, data = json.loads(data[5 : 5 + size]), data[5 + size :]
            if flags & 0x02:
                assert "error" not in message, message
                return messages
            messages.append(message)
        raise AssertionError("the stream ended without an end-of-stream envelope")


@pytest.fixture
def edge_env(postgres_url: str) -> dict[str, str]:
    return {**os.environ, "SWARMEVAL_DATABASE_URL": postgres_url}


@pytest.fixture
def admin(edge_binary: Path, edge_env: dict[str, str]) -> str:
    name = f"root{free_port()}"
    created = subprocess.run(
        [edge_binary, "user", "create", name, "--admin"],
        input=PASSWORD + "\n",
        env=edge_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert created.stdout.strip() == f'created admin "{name}"'
    return name


@pytest.fixture
async def analysis_address(engine: AsyncEngine, object_store: ObjectStore) -> AsyncIterator[str]:
    """The analysis service on a loopback port, with no judge key."""
    async with httpx2.AsyncClient() as http:
        service = AnalysisService(engine=engine, store=object_store, http=http, gateway=None)
        await service.start()
        server = grpc.aio.server()
        add_AnalysisServiceServicer_to_server(service, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        yield f"127.0.0.1:{port}"
        await server.stop(None)
        await service.close()


@pytest.fixture
def edge_process(
    edge_binary: Path, edge_env: dict[str, str], platform: Platform, analysis_address: str
) -> Iterator[str]:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            edge_binary,
            "serve",
            "--listen",
            f"127.0.0.1:{port}",
            "--public-url",
            url,
            "--control",
            platform.control_address,
            "--analysis",
            analysis_address,
        ],
        env=edge_env,
    )
    try:
        wait_listening(proc, port)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
async def edge(edge_process: str) -> AsyncIterator[Edge]:
    async with httpx2.AsyncClient(timeout=60) as http:
        yield Edge(edge_process, http)


async def test_a_case_submitted_through_edge_runs_and_streams_back(
    platform: Platform, edge: Edge, admin: str, tmp_path: Path
) -> None:
    backend = platform.backend
    backend.reply(completion("", tool_calls=[tool_call("shell", json.dumps({"cmd": DEV_CMD}))]))
    backend.reply(completion("all done"))
    backend.reply(completion("ok"))

    signed_in = await edge.call(
        "AuthService/LoginForToken",
        {"username": admin, "password": PASSWORD, "tokenName": "e2e"},
    )
    assert signed_in.status_code == 200, signed_in.text
    auth = {"Authorization": f"Bearer {signed_in.json()['token']}"}

    bundle = base64.b64encode(pack(write_case(tmp_path / "case"))).decode()
    submitted = await edge.call(
        "RunService/SubmitRuns",
        {"caseBundle": bundle, "models": {"default": {"names": ["mock-model"]}}},
        **auth,
    )
    assert submitted.status_code == 200, submitted.text
    (run_id,) = submitted.json()["runIds"]
    outcomes = await platform.worker.drain()
    assert outcomes[run_id].status == "done", outcomes[run_id].error

    got = (await edge.call("RunService/GetRun", {"runId": run_id}, **auth)).json()["run"]
    assert (got["status"], got["submittedBy"], got["workspace"]) == ("done", admin, "ws_e2e")
    assert "ownerId" not in got

    events = await edge.stream("RunService/StreamEvents", {"runId": run_id}, **auth)
    seqs = [int(e["seq"]) for e in events]
    assert seqs == list(range(1, len(seqs) + 1))
    assert any(e["type"] == "score" for e in events)
    assert all(json.loads(e["payloadJson"]) for e in events)


async def test_a_browser_session_needs_its_origin_and_mistakes_come_back_as_connect_errors(
    edge: Edge, admin: str
) -> None:
    login = await edge.call(
        "AuthService/Login", {"username": admin, "password": PASSWORD}, Origin=edge.url
    )
    assert login.status_code == 200, login.text
    cookie = login.headers["set-cookie"].split(";", 1)[0]
    # Send the cookie only where a call names it.
    edge.http.cookies.clear()
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=Strict" in login.headers["set-cookie"]

    me = await edge.call("AuthService/WhoAmI", {}, Origin=edge.url, Cookie=cookie)
    assert me.json()["user"] == {
        "username": admin,
        "role": "ROLE_ADMIN",
        "createdAt": me.json()["user"]["createdAt"],
    }
    no_origin = await edge.call("AuthService/WhoAmI", {}, Cookie=cookie)
    assert (no_origin.status_code, no_origin.json()["code"]) == (403, "permission_denied")

    missing = await edge.call(
        "RunService/GetRun", {"runId": "nope"}, Origin=edge.url, Cookie=cookie
    )
    assert (missing.status_code, missing.json()["code"]) == (404, "not_found")
    assert "nope" in missing.json()["message"]

    anonymous = await edge.call("RunService/ListRuns", {})
    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "unauthenticated")


@pytest.fixture(scope="session")
def swarm_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return go_build("swarm", tmp_path_factory.mktemp("swarm-bin"))


@dataclass
class Cli:
    binary: Path
    env: dict[str, str]

    async def start(self, *args: str, stdin: str = "") -> "asyncio.subprocess.Process":
        proc = await asyncio.create_subprocess_exec(
            self.binary,
            *args,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin is not None
        proc.stdin.write(stdin.encode())
        proc.stdin.close()
        return proc

    async def run(self, *args: str, stdin: str = "") -> tuple[int, str, str]:
        return await finish(await self.start(*args, stdin=stdin))


async def finish(proc: "asyncio.subprocess.Process") -> tuple[int, str, str]:
    out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()


@pytest.fixture
def cli(swarm_binary: Path, tmp_path: Path) -> Cli:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
    return Cli(swarm_binary, {**env, "SWARM_CONFIG": str(tmp_path / "swarm" / "config.yaml")})


async def test_the_cli_signs_in_runs_a_case_and_follows_it(
    platform: Platform, edge: Edge, admin: str, cli: Cli, tmp_path: Path
) -> None:
    platform.backend.respond = lambda _: completion("done")
    code, out, err = await cli.run(
        "login", "--endpoint", edge.url, "-u", admin, stdin=PASSWORD + "\n"
    )
    assert code == 0, err
    assert f"as {admin}" in out
    assert (await cli.run("whoami"))[1].startswith(f"{admin} (admin) at {edge.url}")

    code, out, _ = await cli.run("models")
    assert (code, out.split()) == (0, ["minimax-m3", "mock-model", "qwen3-8b"])
    code, _, err = await cli.run("run", str(write_case(tmp_path / "unchosen")))
    assert code == 1 and "choose the models to run with -m" in err, err
    following = await cli.start(
        "run", str(write_case(tmp_path / "case")), "-m", "mock-model", "--follow"
    )
    assert following.stdout is not None
    # Drain only once the run is queued: before that the worker finds nothing to claim.
    submitted = (await asyncio.wait_for(following.stdout.readline(), timeout=60)).decode()
    outcomes = await platform.worker.drain()
    code, rest, err = await finish(following)
    out = submitted + rest

    assert code == 0, err
    (run_id,) = outcomes
    assert outcomes[run_id].status == "done"
    lines = out.splitlines()
    assert lines[0].startswith("submission ")
    assert any(line.startswith("[") and " #1 " in line for line in lines)
    assert any(" score " in line for line in lines)
    assert lines[-1].split()[:2] == [run_id, "done"]

    code, out, _ = await cli.run("runs", "get", run_id, "--json")
    assert code == 0
    got = json.loads(out)
    assert (got["status"], got["submittedBy"]) == ("done", admin)

    code, out, _ = await cli.run("events", run_id)
    assert code == 0
    assert out.splitlines()[0].startswith("[") and " #1 " in out.splitlines()[0]


async def test_the_cli_submits_a_suite_whole_and_reports_mistakes(
    platform: Platform, edge: Edge, admin: str, cli: Cli, tmp_path: Path
) -> None:
    platform.backend.respond = lambda _: completion("done")
    await cli.run("login", "--endpoint", edge.url, "-u", admin, stdin=PASSWORD + "\n")
    write_case(tmp_path / "case")
    suite = tmp_path / "suites" / "s.yaml"
    suite.parent.mkdir()
    suite.write_text(
        "schema_version: 2\nid: e2e\nmodels: [mock-model]\nepochs: 1\n"
        "cases:\n  - path: ../case\n  - path: ../case\n"
    )

    code, out, err = await cli.run("run", str(suite))
    assert code == 0, err
    label = re.search(r"^suite (e2e\.[0-9a-f]{8}): 2 runs$", out, re.M)
    assert label is not None, out
    await platform.worker.drain()
    code, out, _ = await cli.run("runs", "list", "--suite", label.group(1), "--json")
    assert [r["status"] for r in json.loads(out)["runs"]] == ["done", "done"]

    suite.write_text(
        "schema_version: 2\nid: e2e\nmodels: [mock-model]\ncases:\n  - path: ../case\n"
        "    epochs: 0\n"
    )
    code, _, err = await cli.run("run", str(suite))
    assert code == 1
    assert "cases.0.epochs" in err or "cases[0]" in err, err

    # Review on #20: a link out of the case directory goes up as a link, and is refused.
    leaky = write_case(tmp_path / "leaky")
    (tmp_path / "secret.txt").write_text("SECRET")
    (leaky / "prompts" / "leak.md").symlink_to("../../secret.txt")
    code, _, err = await cli.run("run", str(leaky), "-m", "mock-model")
    assert code == 1
    assert "leak.md" in err and "outside the destination" in err, err

    code, _, err = await cli.run("runs", "get", "nope")
    assert (code, "no run `nope`" in err) == (1, True), err

    code, out, _ = await cli.run("logout")
    assert (code, out.strip()) == (0, "token revoked")
    code, _, err = await cli.run("whoami")
    assert code == 1 and "swarm login" in err


async def test_the_cli_keeps_a_case_in_the_library_and_runs_a_revision(
    platform: Platform, edge: Edge, admin: str, cli: Cli, tmp_path: Path
) -> None:
    """M4 spec, Plan step 4: push, list, revisions, pull, run by revision, archive."""
    platform.backend.respond = lambda _: completion("done")
    await cli.run("login", "--endpoint", edge.url, "-u", admin, stdin=PASSWORD + "\n")
    workspace = f"ws-{secrets.token_hex(4)}"
    name = f"{workspace}/smoke"
    case = write_case(tmp_path / "case")
    spec = yaml.safe_load((case / "case.yaml").read_text())
    (case / "case.yaml").write_text(yaml.safe_dump({**spec, "workspace": workspace}))
    (case / "scorers" / "check.sh").chmod(0o755)

    code, out, err = await cli.run("case", "push", str(case), "-m", "first")
    assert (code, out.strip()) == (0, f"{name}@1 pushed"), err
    assert (await cli.run("case", "push", str(case)))[1].strip() == f"{name}@1 unchanged, already"
    (case / "task.md").write_text("Make the tests pass, twice.")
    assert (await cli.run("case", "push", str(case)))[1].strip() == f"{name}@2 pushed"

    code, out, _ = await cli.run("case", "list", "--workspace", workspace, "--json")
    (listed,) = json.loads(out)["cases"]
    assert (listed["caseId"], listed["latest"]["revision"], listed["latest"]["createdBy"]) == (
        "smoke",
        2,
        admin,
    )
    code, out, _ = await cli.run("case", "revisions", name, "--json")
    assert [(r["revision"], r.get("note", "")) for r in json.loads(out)["revisions"]] == [
        (2, ""),
        (1, "first"),
    ]

    pulled = tmp_path / "pulled"
    code, out, err = await cli.run("case", "pull", f"{name}@1", str(pulled))
    assert code == 0, err
    assert (pulled / "task.md").read_text() == "Make the tests pass."
    assert (pulled / "scorers" / "check.sh").stat().st_mode & 0o777 == 0o755
    assert sorted(p.name for p in pulled.iterdir()) == sorted(p.name for p in case.iterdir())
    code, _, err = await cli.run("case", "pull", f"{name}@1", str(pulled))
    assert code == 1 and "not empty" in err

    code, out, err = await cli.run("run", "--case", f"{name}@1", "-m", "mock-model")
    assert code == 0, err
    assert re.match(r"submission [0-9a-f]{8}: 1 runs of revision 1\n", out), out
    run_id = out.splitlines()[1]
    outcomes = await platform.worker.drain()
    assert outcomes[run_id].status == "done", outcomes[run_id].error
    got = json.loads((await cli.run("runs", "get", run_id, "--json"))[1])
    assert (got["caseRevision"], got["workspace"], got["status"]) == (1, workspace, "done")

    # The console's edit, as it sends it: a stale base is 409 `aborted`, naming the newest.
    signed_in = await edge.call(
        "AuthService/LoginForToken", {"username": admin, "password": PASSWORD, "tokenName": "ui"}
    )
    auth = {"Authorization": f"Bearer {signed_in.json()['token']}"}
    edit = {
        "workspace": workspace,
        "caseId": "smoke",
        "baseRevision": 1,
        "changes": [{"path": "task.md", "content": base64.b64encode(b"From the web.").decode()}],
    }
    stale = await edge.call("CaseService/UpdateCaseFiles", edit, **auth)
    assert (stale.status_code, stale.json()["code"]) == (409, "aborted"), stale.text
    assert "is at revision 2" in stale.json()["message"]
    saved = await edge.call("CaseService/UpdateCaseFiles", {**edit, "baseRevision": 2}, **auth)
    assert saved.status_code == 200, saved.text
    assert (saved.json()["revision"]["revision"], saved.json()["revision"]["createdBy"]) == (
        3,
        admin,
    )

    assert (await cli.run("case", "archive", name))[0] == 0
    code, _, err = await cli.run("run", "--case", name, "-m", "mock-model")
    assert code == 1 and "archived" in err, err
    assert json.loads((await cli.run("case", "list", "--workspace", workspace, "--json"))[1]) == {}
    assert (await cli.run("case", "unarchive", name))[0] == 0
    code, out, _ = await cli.run("case", "list", "--workspace", workspace)
    assert f"{name}  3" in out, out


async def test_the_cli_queries_reports_and_exports_a_finished_run(
    platform: Platform, edge: Edge, admin: str, cli: Cli, tmp_path: Path
) -> None:
    """M4 spec, Plan step 5: `swarm query`, `report`, and `export` through edge and the
    analysis service, over a run the worker exported."""
    backend = platform.backend
    backend.reply(completion("", tool_calls=[tool_call("shell", json.dumps({"cmd": DEV_CMD}))]))
    backend.reply(completion("all done"))
    backend.reply(completion("ok"))
    await cli.run("login", "--endpoint", edge.url, "-u", admin, stdin=PASSWORD + "\n")
    code, out, err = await cli.run("run", str(write_case(tmp_path / "case")), "-m", "mock-model")
    assert code == 0, err
    submission = out.split()[1].rstrip(":")
    run_id = out.splitlines()[1]
    outcomes = await platform.worker.drain()
    assert outcomes[run_id].status == "done", outcomes[run_id].error

    sql = (
        "SELECT r.run_id, r.status, count(*) AS events FROM runs r JOIN events e USING (run_id) "
        f"WHERE r.submission_id = '{submission}' GROUP BY ALL"
    )
    code, out, err = await cli.run("query", sql)
    assert code == 0, err
    header, row = (line.split() for line in out.splitlines())
    assert header == ["run_id", "status", "events"]
    assert row[:2] == [run_id, "done"] and int(row[2]) > 3
    code, out, _ = await cli.run("query", sql, "--csv")
    assert out.splitlines()[0] == "run_id,status,events" and out.splitlines()[1].startswith(run_id)
    code, out, _ = await cli.run("query", sql, "--json")
    assert json.loads(out)["status"] == "done"
    tools = f"SELECT seq FROM events WHERE run_id = '{run_id}' ORDER BY seq"
    code, out, err = await cli.run("query", tools, "--max-rows", "2")
    assert code == 0 and out.split() == ["seq", "1", "2"] and "cut at 2 rows" in err
    code, _, err = await cli.run("query", "SELECT * FROM read_csv('/etc/passwd')")
    assert code == 1 and "swarm: query: " in err and "disabled by configuration" in err, err
    code, _, err = await cli.run("query", "SET enable_external_access = true")
    assert code == 1 and "one SELECT" in err, err

    code, out, err = await cli.run("report", "--submission", submission)
    assert code == 0, err
    assert "smoke@" in out and "| 1 |" in out.replace("1.00", "1")
    code, out, _ = await cli.run("report", "--submission", submission, "--json")
    report = json.loads(out)
    assert report["coverage"][0]["done"] == 1 and report["rates"][0]["epochs"] == 1
    code, _, err = await cli.run("report", "--compare", "nonsense")
    assert code == 1 and "AXIS=A,B" in err

    target = tmp_path / "out.eval"
    code, out, err = await cli.run("export", run_id, "-o", str(target))
    assert code == 0, err
    assert target.read_bytes() == platform.store.get(export_key(run_id))
    assert out.strip() == f"{target}: {target.stat().st_size} bytes"
    code, _, err = await cli.run("export", run_id, "-o", str(target))
    assert code == 1 and "not overwritten" in err
    parquet = await cli.start("export", run_id, "--format", "parquet", "-o", "-")
    raw, _ = await asyncio.wait_for(parquet.communicate(), timeout=60)
    assert raw == platform.store.get(events_key(run_id))
    missing = tmp_path / "missing.eval"
    code, _, err = await cli.run("export", "smoke.00000000.v0.e1", "-o", str(missing))
    assert code == 1 and "no `runs/smoke.00000000.v0.e1/sample.eval`" in err, err
    assert not missing.exists()

    # The console's calls, as it sends them.
    signed_in = await edge.call(
        "AuthService/LoginForToken", {"username": admin, "password": PASSWORD, "tokenName": "ui"}
    )
    auth = {"Authorization": f"Bearer {signed_in.json()['token']}"}
    calls = await edge.call("AnalysisService/SearchToolCalls", {"runIds": [run_id]}, **auth)
    assert calls.status_code == 200, calls.text
    assert [c["tool"] for c in calls.json()["calls"]] == ["shell"]
    started = await edge.call(
        "AnalysisService/StartRuleScan",
        {
            "rulesYaml": "schema_version: 1\nrules:\n  - id: done\n    keyword: all done\n",
            "runIds": [run_id],
        },
        **auth,
    )
    assert started.status_code == 200, started.text
    job = started.json()["job"]
    assert job["createdBy"] == admin
    for _ in range(200):
        got = await edge.call("AnalysisService/GetJob", {"jobId": job["jobId"]}, **auth)
        job = got.json()["job"]
        if job["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.05)
    assert job["status"] == "done" and job["ruleScan"]["totalMatches"] >= 1, job
    last_event = (await edge.stream("RunService/StreamEvents", {"runId": run_id}, **auth))[-1]
    traced = await edge.call(
        "AnalysisService/GetTrace", {"runId": run_id, "eventId": last_event["eventId"]}, **auth
    )
    assert traced.status_code == 200, traced.text
    links = traced.json()["links"]
    assert links[0]["seq"] == "1" and links[-1]["eventId"] == last_event["eventId"]
    judged = await edge.call(
        "AnalysisService/Judge", {"runId": run_id, "question": "q", "model": "m"}, **auth
    )
    assert (judged.status_code, judged.json()["code"]) == (400, "failed_precondition")
