"""Mutual TLS for the gateway's HTTP API (docs/architecture.md#service-identity)."""

import asyncio
import json
import logging
import ssl
from collections.abc import Sequence
from typing import Any

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from swarmeval.mtls import Identity, refusal

log = logging.getLogger(__name__)


def _refuse(why: str) -> Any:
    body = json.dumps({"error": {"code": "caller_not_allowed", "message": why}}).encode()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app


def caller_check(allowed: Sequence[str]) -> type[asyncio.Protocol]:
    """uvicorn's HTTP/1.1 protocol, answering 403 on every connection whose client certificate
    does not name an allowed service.

    The check sits on the connection, not in an ASGI middleware, because uvicorn does not pass
    the peer certificate to the application. The TLS handshake (`CERT_REQUIRED`) has already
    refused clients without a certificate from the CA."""

    class CallerCheck(H11Protocol):
        def connection_made(self, transport: asyncio.Transport) -> None:
            super().connection_made(transport)
            certificate: dict[str, Any] = transport.get_extra_info("peercert") or {}
            names: Sequence[tuple[str, str]] = certificate.get("subjectAltName", ())
            uris = [value for kind, value in names if kind == "URI"]
            why = refusal(uris, allowed)
            if why is not None:
                log.warning("refused a connection from %s: %s", self.client, why)
                # Every request of this connection gets the refusal instead of the gateway.
                self.app = _refuse(why)

    return CallerCheck


def uvicorn_config(
    app: Any, host: str, port: int, mtls: Identity | None, allowed: Sequence[str]
) -> uvicorn.Config:
    if mtls is None:
        return uvicorn.Config(app, host=host, port=port)
    return uvicorn.Config(
        app,
        host=host,
        port=port,
        http=caller_check(allowed),
        ssl_certfile=mtls.cert,
        ssl_keyfile=mtls.key,
        ssl_ca_certs=str(mtls.ca),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )
