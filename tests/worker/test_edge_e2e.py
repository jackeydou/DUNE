"""edge in front of the real Control API: the built `edge` binary, a user created with
`edge user create`, a case submitted over the public API as the console and CLI will submit it
(Connect's JSON protocol), run by the worker, and its events streamed back through edge
(M4 spec, Plan step 2)."""

import asyncio
import base64
import json
import os
import re
import struct
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest

from swarmeval.control.bundles import pack
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
def edge_process(edge_binary: Path, edge_env: dict[str, str], platform: Platform) -> Iterator[str]:
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
    submitted = await edge.call("RunService/SubmitRuns", {"caseBundle": bundle}, **auth)
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

    following = await cli.start("run", str(write_case(tmp_path / "case")), "--follow")
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
        "schema_version: 1\nid: e2e\nepochs: 1\ncases:\n  - path: ../case\n  - path: ../case\n"
    )

    code, out, err = await cli.run("run", str(suite))
    assert code == 0, err
    label = re.search(r"^suite (e2e\.[0-9a-f]{8}): 2 runs$", out, re.M)
    assert label is not None, out
    await platform.worker.drain()
    code, out, _ = await cli.run("runs", "list", "--suite", label.group(1), "--json")
    assert [r["status"] for r in json.loads(out)["runs"]] == ["done", "done"]

    suite.write_text("schema_version: 1\nid: e2e\ncases:\n  - path: ../case\n    epochs: 0\n")
    code, _, err = await cli.run("run", str(suite))
    assert code == 1
    assert "cases.0.epochs" in err or "cases[0]" in err, err

    # Review on #20: a link out of the case directory goes up as a link, and is refused.
    leaky = write_case(tmp_path / "leaky")
    (tmp_path / "secret.txt").write_text("SECRET")
    (leaky / "prompts" / "leak.md").symlink_to("../../secret.txt")
    code, _, err = await cli.run("run", str(leaky))
    assert code == 1
    assert "leak.md" in err and "outside the destination" in err, err

    code, _, err = await cli.run("runs", "get", "nope")
    assert (code, "no run `nope`" in err) == (1, True), err

    code, out, _ = await cli.run("logout")
    assert (code, out.strip()) == (0, "token revoked")
    code, _, err = await cli.run("whoami")
    assert code == 1 and "swarm login" in err
