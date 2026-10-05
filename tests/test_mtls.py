"""Mutual TLS between services: who each server accepts, and that a server without a
certificate stays on loopback (M4 spec decision 10)."""

import argparse
import asyncio
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import grpc
import httpx2
import pytest
import uvicorn

from swarmeval import mtls
from swarmeval.control import server as control_server
from swarmeval.control import suite
from swarmeval.gateway.model import server as gateway_server
from swarmeval.gateway.model.app import create_app
from swarmeval.gateway.model.config import GatewayConfig
from swarmeval.gateway.model.recorder import Attachments
from swarmeval.gateway.model.tls import uvicorn_config
from swarmeval.gateway.model.upstream import Upstreams
from swarmeval.mtls import Identity
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import (
    ControlServiceServicer,
    ControlServiceStub,
    add_ControlServiceServicer_to_server,
)
from swarmeval.proto.swarmeval.modelgw.v1 import recorder_pb2 as recorder_pb
from swarmeval.proto.swarmeval.modelgw.v1.recorder_pb2_grpc import (
    RecorderServiceServicer,
    RecorderServiceStub,
    add_RecorderServiceServicer_to_server,
)
from swarmeval.worker import server as worker_server
from tests.certs import TestCA
from tests.containers import free_port

SERVICES = (
    mtls.EDGE,
    mtls.CONTROL,
    mtls.WORKER,
    mtls.ANALYSIS,
    mtls.MODEL_GATEWAY,
    mtls.SANDBOXD,
    mtls.OPERATOR,
)


@pytest.fixture(scope="module")
def ca(tmp_path_factory: pytest.TempPathFactory) -> TestCA:
    return TestCA(tmp_path_factory.mktemp("ca"))


@pytest.fixture(scope="module")
def identities(ca: TestCA) -> dict[str, Identity]:
    return {service: ca.issue(service) for service in SERVICES}


@pytest.fixture(scope="module")
def stranger(tmp_path_factory: pytest.TempPathFactory) -> Identity:
    """The edge certificate of another deployment's CA."""
    return TestCA(tmp_path_factory.mktemp("other-ca")).issue(mtls.EDGE)


class _Control(ControlServiceServicer):
    """Answers the two shapes of call the Control API has; a call that arrives got through."""

    async def GetRun(self, request: pb.GetRunRequest, context: Any) -> pb.GetRunResponse:
        return pb.GetRunResponse(run=pb.Run(run_id=request.run_id))

    async def StreamEvents(
        self, request: pb.StreamEventsRequest, context: Any
    ) -> AsyncIterator[pb.StreamEventsResponse]:
        yield pb.StreamEventsResponse(seq=1)
        yield pb.StreamEventsResponse(seq=2)


class _Recorder(RecorderServiceServicer):
    async def Attach(
        self, request_iterator: AsyncIterator[recorder_pb.AttachRequest], context: Any
    ) -> AsyncIterator[recorder_pb.AttachResponse]:
        async for _ in request_iterator:
            yield recorder_pb.AttachResponse(attached=recorder_pb.Attached())


@pytest.fixture
async def control(identities: dict[str, Identity]) -> AsyncIterator[str]:
    """A Control API stand-in, bound and guarded as `swarmeval-control` binds the real one."""
    identity = identities[mtls.CONTROL]
    server = grpc.aio.server(interceptors=mtls.interceptors(identity, control_server.CALLERS))
    add_ControlServiceServicer_to_server(
        _Control(),  # pyright: ignore[reportAbstractUsage]
        server,
    )
    port = mtls.add_port(server, "127.0.0.1:0", identity)
    await server.start()
    yield f"localhost:{port}"
    await server.stop(None)


async def _get_run(address: str, identity: Identity | None) -> grpc.StatusCode:
    async with mtls.channel(address, identity) as channel:
        try:
            await ControlServiceStub(channel).GetRun(pb.GetRunRequest(run_id="r"), timeout=10)
        except grpc.aio.AioRpcError as err:
            return err.code()
    return grpc.StatusCode.OK


async def test_control_api_serves_edge_and_operators_only(
    control: str, identities: dict[str, Identity], stranger: Identity
) -> None:
    for service in SERVICES:
        expected = (
            grpc.StatusCode.OK
            if service in (mtls.EDGE, mtls.OPERATOR)
            else grpc.StatusCode.PERMISSION_DENIED
        )
        assert await _get_run(control, identities[service]) == expected, service
    # No certificate, and a certificate another CA signed, never reach the interceptor: the
    # handshake fails.
    assert await _get_run(control, None) == grpc.StatusCode.UNAVAILABLE
    assert await _get_run(control, stranger) == grpc.StatusCode.UNAVAILABLE


async def test_a_refusal_says_who_called_and_who_may(
    control: str, identities: dict[str, Identity]
) -> None:
    async with mtls.channel(control, identities[mtls.WORKER]) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await ControlServiceStub(channel).GetRun(pb.GetRunRequest(run_id="r"))
    assert refused.value.details() == (
        "service `worker` may not call this one; it accepts edge, operator."
    )


async def test_streaming_calls_are_checked_too(
    control: str, identities: dict[str, Identity]
) -> None:
    request = pb.StreamEventsRequest(run_id="r")
    async with mtls.channel(control, identities[mtls.EDGE]) as channel:
        events = [e.seq async for e in ControlServiceStub(channel).StreamEvents(request)]
    assert events == [1, 2]
    async with mtls.channel(control, identities[mtls.SANDBOXD]) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            _ = [e async for e in ControlServiceStub(channel).StreamEvents(request)]
    assert refused.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_a_certificate_that_names_no_single_service_is_refused(
    control: str, ca: TestCA
) -> None:
    for uris in (
        list[str](),
        ["spiffe://swarmeval/edge", "spiffe://swarmeval/operator"],
        ["spiffe://elsewhere/edge"],
        ["spiffe://swarmeval/edge/extra"],
        ["https://swarmeval/edge"],
    ):
        assert await _get_run(control, ca.issue("odd", uris=uris)) == (
            grpc.StatusCode.PERMISSION_DENIED
        ), uris


async def test_recorder_serves_workers_and_analysis_only(identities: dict[str, Identity]) -> None:
    identity = identities[mtls.MODEL_GATEWAY]
    server = grpc.aio.server(interceptors=mtls.interceptors(identity, gateway_server.CALLERS))
    add_RecorderServiceServicer_to_server(_Recorder(), server)
    port = mtls.add_port(server, "127.0.0.1:0", identity)
    await server.start()

    async def attach(caller: Identity | None) -> grpc.StatusCode:
        async def requests() -> AsyncIterator[recorder_pb.AttachRequest]:
            yield recorder_pb.AttachRequest()

        async with mtls.channel(f"localhost:{port}", caller) as channel:
            try:
                async for _ in RecorderServiceStub(channel).Attach(requests(), timeout=10):
                    pass
            except grpc.aio.AioRpcError as err:
                return err.code()
        return grpc.StatusCode.OK

    try:
        assert await attach(identities[mtls.WORKER]) == grpc.StatusCode.OK
        assert await attach(identities[mtls.ANALYSIS]) == grpc.StatusCode.OK
        assert await attach(identities[mtls.EDGE]) == grpc.StatusCode.PERMISSION_DENIED
        assert await attach(None) == grpc.StatusCode.UNAVAILABLE
    finally:
        await server.stop(None)


@pytest.fixture
async def gateway_http(identities: dict[str, Identity]) -> AsyncIterator[str]:
    """model-gateway's HTTP API, served as `swarmeval-model-gateway` serves it."""
    config = GatewayConfig.model_validate(
        {
            "backends": {"b": {"base_url": "http://backend.invalid/v1"}},
            "models": {"m": {"backend": "b", "upstream_model": "Org/M"}},
        }
    )
    upstreams = Upstreams(config)
    port = free_port()
    web = uvicorn.Server(
        uvicorn_config(
            create_app(Attachments(), upstreams),
            "127.0.0.1",
            port,
            identities[mtls.MODEL_GATEWAY],
            gateway_server.CALLERS,
        )
    )
    task = asyncio.create_task(web.serve())
    while not web.started:
        await asyncio.sleep(0.01)
    yield f"https://localhost:{port}"
    web.should_exit = True
    await task
    await upstreams.close()


async def _models(url: str, identity: Identity | None) -> httpx2.Response:
    verify = mtls.http_client_tls("--gateway-http", url, identity) if identity else False
    async with httpx2.AsyncClient(verify=verify, timeout=10) as http:
        return await http.get(f"{url}/v1/models")


async def test_gateway_http_serves_workers_and_analysis_only(
    gateway_http: str, identities: dict[str, Identity], stranger: Identity
) -> None:
    for service in (mtls.WORKER, mtls.ANALYSIS):
        response = await _models(gateway_http, identities[service])
        assert response.status_code == 200, service
        assert response.json()["data"][0]["id"] == "m"
    for service in (mtls.EDGE, mtls.CONTROL, mtls.SANDBOXD, mtls.OPERATOR):
        response = await _models(gateway_http, identities[service])
        assert response.status_code == 403, service
        assert response.json()["error"] == {
            "code": "caller_not_allowed",
            "message": f"service `{service}` may not call this one; it accepts worker, analysis.",
        }
    # Without a certificate, or with another CA's, the handshake fails before any request.
    with pytest.raises(httpx2.TransportError):
        await _models(gateway_http, None)
    with pytest.raises(httpx2.TransportError):
        await _models(gateway_http, Identity(stranger.cert, stranger.key, identities["edge"].ca))


async def test_a_client_refuses_a_server_another_ca_signed(
    gateway_http: str, stranger: Identity
) -> None:
    with pytest.raises(httpx2.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        await _models(gateway_http, stranger)


@pytest.mark.parametrize("listen", ["0.0.0.0:7090", ":7090", "10.0.0.5:7090", "control:7090"])
async def test_without_a_certificate_servers_listen_on_loopback_only(listen: str) -> None:
    config = GatewayConfig.model_validate({"backends": {}, "models": {}})
    with pytest.raises(SystemExit, match=f"--listen {listen} is not a loopback address"):
        # Refused before the database or the store is touched.
        await control_server.serve(
            "postgresql://nowhere",
            None,  # pyright: ignore[reportArgumentType]
            listen,
            allow_case_code=False,
            mtls=None,
        )
    with pytest.raises(SystemExit, match=f"--http {listen} is not a loopback address"):
        await gateway_server.serve(config, http=listen, grpc_address="127.0.0.1:0", mtls=None)
    with pytest.raises(SystemExit, match=f"--grpc {listen} is not a loopback address"):
        await gateway_server.serve(config, http="127.0.0.1:0", grpc_address=listen, mtls=None)


@pytest.mark.parametrize("address", ["127.0.0.1:7090", "localhost:7090", "[::1]:7090"])
def test_loopback_addresses_need_no_certificate(address: str) -> None:
    mtls.check_listen("--listen", address, None)


def test_with_a_certificate_any_address_is_allowed(identities: dict[str, Identity]) -> None:
    mtls.check_listen("--listen", "0.0.0.0:7090", identities[mtls.CONTROL])


def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mtls.add_mtls(parser)
    return parser.parse_args(argv)


def test_the_three_files_go_together(tmp_path: Path) -> None:
    assert mtls.identity(_args()) is None
    assert mtls.identity(
        _args("--mtls-cert", "c", "--mtls-key", "k", "--mtls-ca", "a")
    ) == Identity(Path("c"), Path("k"), Path("a"))
    with pytest.raises(SystemExit, match="give all three"):
        mtls.identity(_args("--mtls-cert", "c", "--mtls-key", "k"))
    with pytest.raises(SystemExit, match="give all three"):
        mtls.identity(_args("--mtls-ca", "a"))
    missing = Identity(tmp_path / "tls.crt", tmp_path / "tls.key", tmp_path / "ca.crt")
    with pytest.raises(SystemExit, match="Give the file swarm-certs wrote"):
        missing.server_credentials()
    with pytest.raises(SystemExit, match="Give the files swarm-certs wrote"):
        missing.client_context()


async def test_the_worker_needs_the_gateway_url_to_match_its_certificate(
    identities: dict[str, Identity],
) -> None:
    async def start(url: str, identity: Identity | None) -> None:
        await worker_server.serve(
            "postgresql://nowhere",
            None,  # pyright: ignore[reportArgumentType]
            sandboxd="127.0.0.1:9",
            gateway_http=url,
            gateway_grpc="127.0.0.1:9",
            owner_id="w",
            max_runs=1,
            allow_case_code=False,
            lease_s=30,
            mtls=identity,
        )

    with pytest.raises(SystemExit, match="with --mtls-cert the service is reached over https"):
        await start("http://model-gateway:7080", identities[mtls.WORKER])
    with pytest.raises(SystemExit, match="without --mtls-cert the service is reached over http"):
        await start("https://model-gateway:7080", None)


async def test_the_suite_tool_submits_as_the_operator(
    identities: dict[str, Identity], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`python -m swarmeval.control.suite submit --mtls-…` reaches a Control API that serves
    mutual TLS."""

    class _Suites(ControlServiceServicer):
        async def SubmitSuite(
            self, request: pb.SubmitSuiteRequest, context: Any
        ) -> pb.SubmitSuiteResponse:
            return pb.SubmitSuiteResponse(suite="s.12345678")

    identity = identities[mtls.CONTROL]
    server = grpc.aio.server(interceptors=mtls.interceptors(identity, control_server.CALLERS))
    add_ControlServiceServicer_to_server(
        _Suites(),  # pyright: ignore[reportAbstractUsage]
        server,
    )
    port = mtls.add_port(server, "127.0.0.1:0", identity)
    await server.start()
    try:
        loaded = suite.load_suite(Path("suites/m1_core.yaml"))
        await suite._submit(  # pyright: ignore[reportPrivateUsage]
            loaded, f"localhost:{port}", identities[mtls.OPERATOR]
        )
        assert "suite label: s.12345678" in capsys.readouterr().out
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await suite._submit(  # pyright: ignore[reportPrivateUsage]
                loaded, f"localhost:{port}", identities[mtls.WORKER]
            )
        assert refused.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await server.stop(None)


def _tls12(identity: Identity) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=identity.ca)
    context.load_cert_chain(identity.cert, identity.key)
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


async def test_gateway_http_refuses_tls_1_2(
    gateway_http: str, identities: dict[str, Identity]
) -> None:
    async with httpx2.AsyncClient(verify=_tls12(identities[mtls.WORKER]), timeout=10) as http:
        with pytest.raises(httpx2.ConnectError):
            await http.get(f"{gateway_http}/v1/models")


def test_http_clients_offer_tls_1_3_only(identities: dict[str, Identity]) -> None:
    assert identities[mtls.WORKER].client_context().minimum_version == ssl.TLSVersion.TLSv1_3
