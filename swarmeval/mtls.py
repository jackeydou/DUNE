"""Mutual TLS between services (docs/architecture.md#service-identity).

Every service holds one certificate from the deployment's CA (`swarm-certs`), which names it in a
URI SAN, `spiffe://swarmeval/<service>`. A server started with a certificate accepts only
connections that present one, and only calls from the services on its list. A server started
without one serves plain text, and only on a loopback address.
"""

import argparse
import ipaddress
import logging
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import grpc

log = logging.getLogger(__name__)

EDGE = "edge"
CONTROL = "control"
WORKER = "worker"
ANALYSIS = "analysis"
MODEL_GATEWAY = "model-gateway"
SANDBOXD = "sandboxd"
OPERATOR = "operator"
"""For tools a person runs on the internal network: `swarmeval.control.suite`, grpcurl."""

_URI_PREFIX = "spiffe://swarmeval/"

if TYPE_CHECKING:
    # grpc's classes take type parameters only in the stubs.
    type _Handler = grpc.RpcMethodHandler[Any, Any]


@dataclass(frozen=True)
class Identity:
    """The PEM files a service is started with: its certificate, its key, and the CA."""

    cert: Path
    key: Path
    ca: Path

    def _read(self, path: Path, flag: str) -> bytes:
        try:
            return path.read_bytes()
        except OSError as err:
            raise SystemExit(f"{flag} {path}: {err}. Give the file swarm-certs wrote.") from err

    def server_credentials(self) -> grpc.ServerCredentials:
        """For a gRPC server: clients must present a certificate the CA signed. Which services
        may call is `CallerCheck`'s to decide."""
        return grpc.ssl_server_credentials(
            [(self._read(self.key, "--mtls-key"), self._read(self.cert, "--mtls-cert"))],
            root_certificates=self._read(self.ca, "--mtls-ca"),
            require_client_auth=True,
        )

    def channel_credentials(self) -> grpc.ChannelCredentials:
        """For a gRPC client: presents this service's certificate, and connects only to a
        certificate the CA signed for the host dialed."""
        return grpc.ssl_channel_credentials(
            root_certificates=self._read(self.ca, "--mtls-ca"),
            private_key=self._read(self.key, "--mtls-key"),
            certificate_chain=self._read(self.cert, "--mtls-cert"),
        )

    def client_context(self) -> ssl.SSLContext:
        """The same for an HTTP client."""
        try:
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.ca)
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.load_cert_chain(self.cert, self.key)
        except (OSError, ssl.SSLError) as err:
            raise SystemExit(
                f"--mtls-cert {self.cert}, --mtls-key {self.key}, --mtls-ca {self.ca}: {err}. "
                "Give the files swarm-certs wrote."
            ) from err
        return context


def add_mtls(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mtls-cert",
        type=Path,
        help="this service's certificate from swarm-certs, PEM. With --mtls-key and --mtls-ca",
    )
    parser.add_argument("--mtls-key", type=Path, help="private key of --mtls-cert, PEM")
    parser.add_argument(
        "--mtls-ca",
        type=Path,
        help="the deployment's CA certificate, PEM: only certificates it signed are accepted",
    )


def identity(args: argparse.Namespace) -> Identity | None:
    """The identity the command line names, or None when it names none. A partial set is
    refused: a service with a certificate but no CA could verify nobody."""
    cert: Path | None = args.mtls_cert
    key: Path | None = args.mtls_key
    ca: Path | None = args.mtls_ca
    if cert is None and key is None and ca is None:
        return None
    if cert is None or key is None or ca is None:
        raise SystemExit(
            f"--mtls-cert {cert}, --mtls-key {key}, --mtls-ca {ca}: give all three to use "
            "mutual TLS, or none to stay on loopback in plain text."
        )
    return Identity(cert, key, ca)


def is_loopback(address: str) -> bool:
    """Whether a `host:port` listen address is reachable from this machine only."""
    host, sep, _ = address.rpartition(":")
    if not sep:
        return False
    host = host.removeprefix("[").removesuffix("]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_listen(flag: str, address: str, mtls: Identity | None) -> None:
    """Refuses to serve plain text to the network: without a certificate nothing says who a
    caller is."""
    if mtls is None and not is_loopback(address):
        raise SystemExit(
            f"{flag} {address} is not a loopback address, and the service has no certificate: "
            "it would accept calls from anyone who reached it. Give --mtls-cert, --mtls-key, "
            "and --mtls-ca, or listen on 127.0.0.1."
        )


def add_port(server: grpc.aio.Server, address: str, mtls: Identity | None) -> int:
    """Binds a gRPC server: mutual TLS with a certificate, plain text without. Call
    `check_listen` first."""
    if mtls is None:
        return server.add_insecure_port(address)
    return server.add_secure_port(address, mtls.server_credentials())


def channel(
    address: str, mtls: Identity | None, options: Sequence[tuple[str, Any]] | None = None
) -> grpc.aio.Channel:
    if mtls is None:
        return grpc.aio.insecure_channel(address, options=options)
    return grpc.aio.secure_channel(address, mtls.channel_credentials(), options=options)


def http_client_tls(flag: str, url: str, mtls: Identity | None) -> ssl.SSLContext | bool:
    """What an HTTP client of another service verifies with (httpx's `verify`). The URL's scheme
    must agree: a server with a certificate serves only https, one without only http."""
    scheme = "http" if mtls is None else "https"
    if not url.startswith(f"{scheme}://"):
        raise SystemExit(
            f"{flag} {url}: with{'out' if mtls is None else ''} --mtls-cert the service is "
            f"reached over {scheme}. Give {scheme}://HOST:PORT."
        )
    return True if mtls is None else mtls.client_context()


def service_of(uris: Sequence[str]) -> str | None:
    """The service a verified certificate's URI names say it is, or None when they name no
    service of this deployment."""
    if len(uris) != 1 or not uris[0].startswith(_URI_PREFIX):
        return None
    service = uris[0].removeprefix(_URI_PREFIX)
    return service if service and "/" not in service else None


def refusal(uris: Sequence[str], allowed: Sequence[str]) -> str | None:
    """Why a caller whose certificate carries `uris` may not call a service that accepts
    `allowed`, or None when it may."""
    service = service_of(uris)
    if service is None:
        return (
            f"the caller's certificate names {list(uris)}, not one service of this deployment "
            f"({_URI_PREFIX}<service>). Use a certificate swarm-certs issued."
        )
    if service not in allowed:
        return f"service `{service}` may not call this one; it accepts {', '.join(allowed)}."
    return None


class CallerCheck(grpc.aio.ServerInterceptor):
    """Lets a gRPC call through only when the caller's certificate names an allowed service;
    anything else is `PERMISSION_DENIED`. For servers bound with `Identity.server_credentials`,
    which has already refused callers without a certificate from the CA."""

    def __init__(self, allowed: Sequence[str]) -> None:
        self._allowed = tuple(allowed)

    async def _check(self, context: "grpc.aio.ServicerContext[Any, Any]", method: str) -> None:
        uris = [u.decode() for u in context.auth_context().get("peer_uri", [])]
        why = refusal(uris, self._allowed)
        if why is not None:
            log.warning("refused %s from %s: %s", method, context.peer(), why)
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, why)

    async def intercept_service(
        self,
        continuation: "Callable[[grpc.HandlerCallDetails], Awaitable[_Handler | None]]",
        handler_call_details: grpc.HandlerCallDetails,
    ) -> "_Handler | None":
        handler = await continuation(handler_call_details)
        # None is how gRPC says "no such method"; it answers UNIMPLEMENTED itself.
        if handler is None:
            return None
        method = handler_call_details.method
        check = self._check
        codec = {
            "request_deserializer": handler.request_deserializer,
            "response_serializer": handler.response_serializer,
        }
        # The auth context exists only on the call's context, so the check wraps the behavior.
        # Streaming behaviors here are async generators, as every servicer in this package is.
        if handler.unary_unary is not None:
            unary_unary = cast(Callable[[Any, Any], Awaitable[Any]], handler.unary_unary)

            async def guarded_unary_unary(request: Any, context: Any) -> Any:
                await check(context, method)
                return await unary_unary(request, context)

            return grpc.unary_unary_rpc_method_handler(guarded_unary_unary, **codec)
        if handler.unary_stream is not None:
            unary_stream = cast(Callable[[Any, Any], AsyncIterator[Any]], handler.unary_stream)

            async def guarded_unary_stream(request: Any, context: Any) -> AsyncIterator[Any]:
                await check(context, method)
                async for response in unary_stream(request, context):
                    yield response

            return grpc.unary_stream_rpc_method_handler(guarded_unary_stream, **codec)
        if handler.stream_unary is not None:
            stream_unary = cast(Callable[[Any, Any], Awaitable[Any]], handler.stream_unary)

            async def guarded_stream_unary(requests: Any, context: Any) -> Any:
                await check(context, method)
                return await stream_unary(requests, context)

            return grpc.stream_unary_rpc_method_handler(guarded_stream_unary, **codec)
        stream_stream = cast(Callable[[Any, Any], AsyncIterator[Any]], handler.stream_stream)

        async def guarded_stream_stream(requests: Any, context: Any) -> AsyncIterator[Any]:
            await check(context, method)
            async for response in stream_stream(requests, context):
                yield response

        return grpc.stream_stream_rpc_method_handler(guarded_stream_stream, **codec)


def interceptors(mtls: Identity | None, allowed: Sequence[str]) -> list[grpc.aio.ServerInterceptor]:
    """What a gRPC server that accepts `allowed` is built with."""
    return [] if mtls is None else [CallerCheck(allowed)]
