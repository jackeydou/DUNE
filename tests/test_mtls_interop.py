"""The Go side of mutual TLS against the Python side, with certificates from the real
`swarm-certs`: a Python worker's channel to sandboxd, and edge's connection to a Control API
served by grpcio (M4 spec decision 10)."""

import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import grpc
import httpx2
import pytest

from swarmeval import mtls
from swarmeval.control.server import CALLERS
from swarmeval.mtls import Identity
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import (
    ControlServiceServicer,
    add_ControlServiceServicer_to_server,
)
from swarmeval.proto.swarmeval.sandbox.v1 import sandbox_pb2
from swarmeval.proto.swarmeval.sandbox.v1.sandbox_pb2_grpc import SandboxServiceStub
from tests.containers import free_port, go_build, wait_listening

pytestmark = pytest.mark.docker

PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def binaries(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("mtls-bin")
    for command in ("swarm-certs", "sandboxd", "edge"):
        go_build(command, out)
    return out


@pytest.fixture(scope="module")
def certs(binaries: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("certs")
    subprocess.run([binaries / "swarm-certs", "--out", out], check=True)
    return out


def identity(certs: Path, service: str) -> Identity:
    d = certs / service
    return Identity(cert=d / "tls.crt", key=d / "tls.key", ca=d / "ca.crt")


def mtls_flags(certs: Path, service: str) -> list[str]:
    files = identity(certs, service)
    return [
        "--mtls-cert",
        str(files.cert),
        "--mtls-key",
        str(files.key),
        "--mtls-ca",
        str(files.ca),
    ]


async def test_sandboxd_serves_a_python_worker_and_nobody_else(
    binaries: Path, certs: Path, tmp_path: Path
) -> None:
    port = free_port()
    state = os.path.realpath(tmp_path)
    proc = subprocess.Popen(
        [
            binaries / "sandboxd",
            "--state-dir",
            state,
            "--listen",
            f"127.0.0.1:{port}",
            *mtls_flags(certs, mtls.SANDBOXD),
        ]
    )
    try:
        wait_listening(proc, port)

        async def destroy(caller: Identity | None) -> grpc.StatusCode:
            async with mtls.channel(f"localhost:{port}", caller) as channel:
                try:
                    await SandboxServiceStub(channel).DestroyRun(
                        sandbox_pb2.DestroyRunRequest(run_id="run_nobody_made"), timeout=20
                    )
                except grpc.aio.AioRpcError as err:
                    return err.code()
            return grpc.StatusCode.OK

        # Removing a run sandboxd never made removes nothing, and succeeds.
        assert await destroy(identity(certs, mtls.WORKER)) == grpc.StatusCode.OK
        for service in (mtls.EDGE, mtls.CONTROL, mtls.OPERATOR):
            assert await destroy(identity(certs, service)) == grpc.StatusCode.UNAVAILABLE, service
        assert await destroy(None) == grpc.StatusCode.UNAVAILABLE
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_sandboxd_without_a_certificate_refuses_a_network_address(
    binaries: Path, tmp_path: Path
) -> None:
    done = subprocess.run(
        [binaries / "sandboxd", "--state-dir", tmp_path, "--listen", "0.0.0.0:0"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 1
    assert "is not a loopback address, and sandboxd has no certificate" in done.stderr


class _Control(ControlServiceServicer):
    async def GetRun(self, request: pb.GetRunRequest, context: Any) -> pb.GetRunResponse:
        return pb.GetRunResponse(run=pb.Run(run_id=request.run_id, status="done"))


@pytest.fixture
async def control(certs: Path) -> AsyncIterator[str]:
    files = identity(certs, mtls.CONTROL)
    server = grpc.aio.server(interceptors=mtls.interceptors(files, CALLERS))
    add_ControlServiceServicer_to_server(
        _Control(),  # pyright: ignore[reportAbstractUsage]
        server,
    )
    port = mtls.add_port(server, "127.0.0.1:0", files)
    await server.start()
    yield f"localhost:{port}"
    await server.stop(None)


async def _get_run_through_edge(
    binaries: Path, postgres_url: str, control: str, flags: list[str]
) -> httpx2.Response:
    env = {**os.environ, "SWARMEVAL_DATABASE_URL": postgres_url}
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    name = f"mtls{port}"
    subprocess.run(
        [binaries / "edge", "user", "create", name],
        input=PASSWORD + "\n",
        env=env,
        text=True,
        check=True,
        capture_output=True,
    )
    proc = subprocess.Popen(
        [
            binaries / "edge",
            "serve",
            "--listen",
            f"127.0.0.1:{port}",
            "--public-url",
            url,
            "--control",
            control,
            *flags,
        ],
        env=env,
    )
    try:
        wait_listening(proc, port)
        async with httpx2.AsyncClient(timeout=30) as http:
            signed_in = await http.post(
                f"{url}/swarmeval.api.v1.AuthService/LoginForToken",
                json={"username": name, "password": PASSWORD, "tokenName": "t"},
            )
            assert signed_in.status_code == 200, signed_in.text
            return await http.post(
                f"{url}/swarmeval.api.v1.RunService/GetRun",
                json={"runId": "run_1"},
                headers={"Authorization": f"Bearer {signed_in.json()['token']}"},
            )
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_edge_reaches_a_grpcio_control_api_over_mutual_tls(
    binaries: Path, certs: Path, postgres_url: str, control: str
) -> None:
    response = await _get_run_through_edge(
        binaries, postgres_url, control, mtls_flags(certs, mtls.EDGE)
    )
    assert response.status_code == 200, response.text
    assert response.json()["run"] == {"runId": "run_1", "status": "done"}


async def test_edge_with_another_services_certificate_or_none_reaches_nothing(
    binaries: Path, certs: Path, postgres_url: str, control: str
) -> None:
    for flags in (mtls_flags(certs, mtls.WORKER), list[str]()):
        response = await _get_run_through_edge(binaries, postgres_url, control, flags)
        # edge hides why the Control API did not answer; the reason is in its log.
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "unavailable"
