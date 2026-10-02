"""The worker side of `web_request`: resolve, check, send, record (trajectory-first spec
decision 3, docs/services/orchestrator.md#web_request).

The host is resolved once and every address is checked; the request then goes to the checked
address, with the original name in `Host` and in TLS SNI, so certificates are still verified
against the name and a second resolution cannot point it elsewhere. Every call opens its own
connection: a pooled TLS connection to a shared address could carry another name's request.
"""

import asyncio
import hashlib
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import httpx2

from swarmeval.runtime.records import WebExchange, WebRequest
from swarmeval.sandbox.blobs import BlobStore
from swarmeval.web.addresses import IPAddress, is_public

Resolver = Callable[[str, int], Awaitable[list[str]]]
"""Resolves a host name to the addresses a connection could use, as text."""


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True)
class WebLimits:
    inline_bytes: int = 64 << 10
    """Body bytes the agent is shown, as for sandbox output."""
    body_cap: int = 16 << 20
    """Body bytes read at all; the rest of a longer body is never fetched."""
    request_body_cap: int = 1 << 20


DEFAULT_LIMITS = WebLimits()


class HttpWebClient:
    """One per run. `transport` replaces the network, for tests."""

    def __init__(
        self,
        blobs: BlobStore,
        *,
        limits: WebLimits = DEFAULT_LIMITS,
        resolve: Resolver = system_resolve,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._blobs = blobs
        self._limits = limits
        self._resolve = resolve
        # trust_env=False: a proxy from the environment would connect on the request's
        # behalf, past the address check.
        self._http = httpx2.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            limits=httpx2.Limits(max_keepalive_connections=0),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def request(self, request: WebRequest) -> WebExchange:
        started = time.monotonic()
        exchange = WebExchange(request=request)
        body = request.body.encode() if request.body is not None else None
        if body is not None:
            if len(body) > self._limits.request_body_cap:
                return exchange.model_copy(
                    update={
                        "error": f"the request body is {len(body)} bytes; the limit is "
                        f"{self._limits.request_body_cap}"
                    }
                )
            exchange = exchange.model_copy(update={"request_body_sha256": await self._put(body)})
        try:
            url = httpx2.URL(request.url)
        except httpx2.InvalidURL as err:
            return exchange.model_copy(update={"error": f"invalid URL: {err}"})
        if url.scheme not in ("http", "https") or not url.raw_host:
            return exchange.model_copy(
                update={"error": "only absolute http:// and https:// URLs can be requested"}
            )
        try:
            async with asyncio.timeout(request.timeout_s):
                exchange = await self._send(exchange, url, body)
        except TimeoutError:
            exchange = exchange.model_copy(
                update={"error": f"timed out after {request.timeout_s:g} s"}
            )
        return exchange.model_copy(update={"duration_s": time.monotonic() - started})

    async def _send(
        self, exchange: WebExchange, url: httpx2.URL, body: bytes | None
    ) -> WebExchange:
        host = url.raw_host.decode("ascii")
        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            addresses = [_address(a) for a in await self._resolve(host, port)]
        except OSError as err:
            return exchange.model_copy(update={"error": f"could not resolve {host}: {err}"})
        refused = [a for a in addresses if not is_public(a)]
        if refused or not addresses:
            reason = (
                f"{host} resolves to non-public address {refused[0]}"
                if refused
                else f"{host} resolves to no address"
            )
            return exchange.model_copy(update={"refused": reason})
        # IPv4 first: a worker without IPv6 egress would fail every dual-stack host otherwise.
        target = min(addresses, key=lambda a: a.version)
        headers = list(exchange.request.headers)
        if not any(name.lower() == "host" for name, _ in headers):
            headers.append(("Host", url.netloc.decode("ascii")))
        try:
            async with self._http.stream(
                exchange.request.method,
                url.copy_with(host=str(target)),
                headers=headers,
                content=body,
                extensions={"sni_hostname": host},
                timeout=exchange.request.timeout_s,
            ) as response:
                data, capped = await self._read(response)
                status, response_headers = response.status_code, response.headers.multi_items()
                charset = response.charset_encoding
        except httpx2.HTTPError as err:
            return exchange.model_copy(
                update={"address": str(target), "error": f"{type(err).__name__}: {err}"}
            )
        shown = data[: self._limits.inline_bytes]
        return exchange.model_copy(
            update={
                "address": str(target),
                "status": status,
                "response_headers": tuple(response_headers),
                "response_body_size": len(data),
                "response_body_sha256": await self._put(data) if data else None,
                "response_body_capped": capped,
                "body": _text(shown, charset),
                "body_bytes": len(shown),
            }
        )

    async def _read(self, response: httpx2.Response) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            room = self._limits.body_cap - size
            if len(chunk) > room:
                chunks.append(chunk[:room])
                return b"".join(chunks), True
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks), False

    async def _put(self, data: bytes) -> str:
        sha256 = hashlib.sha256(data).hexdigest()
        await self._blobs.put(sha256, data)
        return sha256


def _address(text: str) -> IPAddress:
    """getaddrinfo appends a zone to link-local IPv6 addresses (`fe80::1%en0`)."""
    return ipaddress.ip_address(text.split("%", 1)[0])


def _text(data: bytes, charset: str | None) -> str:
    """Postgres `jsonb` rejects NUL, so it never reaches an event."""
    try:
        text = data.decode(charset or "utf-8", errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    return text.replace("\x00", "�")
