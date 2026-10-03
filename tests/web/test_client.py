"""`HttpWebClient` against a mock transport and a fake resolver: nothing here touches the
network."""

import asyncio
import hashlib
import socket
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field

import httpx2

from swarmeval.runtime.records import WebRequest
from swarmeval.web import HttpWebClient, WebLimits
from swarmeval.web.client import DEFAULT_LIMITS

PUBLIC = "93.184.215.14"


@dataclass
class FakeBlobs:
    blobs: dict[str, bytes] = field(default_factory=dict[str, bytes])

    async def put(self, sha256: str, data: bytes) -> None:
        assert hashlib.sha256(data).hexdigest() == sha256
        self.blobs[sha256] = data


def resolving(*addresses: str) -> Callable[[str, int], Awaitable[list[str]]]:
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


Handler = Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]]


def client(
    handler: Handler,
    *,
    addresses: tuple[str, ...] = (PUBLIC,),
    limits: WebLimits = DEFAULT_LIMITS,
) -> tuple[HttpWebClient, FakeBlobs, list[httpx2.Request]]:
    blobs = FakeBlobs()
    seen: list[httpx2.Request] = []

    async def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return await handler(request)

    web = HttpWebClient(
        blobs, limits=limits, resolve=resolving(*addresses), transport=httpx2.MockTransport(record)
    )
    return web, blobs, seen


def ok(body: bytes = b"hello", headers: dict[str, str] | None = None) -> Handler:
    async def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=body, headers=headers)

    return handle


async def test_the_request_goes_to_the_checked_address_with_the_name_in_host_and_sni() -> None:
    web, blobs, seen = client(ok(b"hello", headers={"content-type": "text/plain"}))

    exchange = await web.request(
        WebRequest(method="POST", url="https://example.com:8443/a?b=1", body="payload")
    )

    (sent,) = seen
    assert str(sent.url) == f"https://{PUBLIC}:8443/a?b=1"
    assert sent.headers["host"] == "example.com:8443"
    assert sent.extensions["sni_hostname"] == "example.com"
    assert sent.content == b"payload"
    assert (exchange.address, exchange.status, exchange.body) == (PUBLIC, 200, "hello")
    assert ("content-type", "text/plain") in exchange.response_headers
    assert exchange.response_body_sha256 is not None
    assert blobs.blobs[exchange.response_body_sha256] == b"hello"
    assert exchange.request_body_sha256 is not None
    assert blobs.blobs[exchange.request_body_sha256] == b"payload"
    assert exchange.refused is None and exchange.error is None


async def test_any_non_public_address_refuses_the_request_before_it_is_sent() -> None:
    for addresses in [("127.0.0.1",), ("169.254.169.254",), (PUBLIC, "10.0.0.5"), ("::1",), ()]:
        web, _, seen = client(ok(), addresses=addresses)

        exchange = await web.request(WebRequest(method="GET", url="http://internal.test/"))

        assert seen == []
        assert exchange.refused is not None, addresses
        assert exchange.address is None


async def test_an_ip_literal_is_checked_like_a_name() -> None:
    web, _, seen = client(ok(), addresses=("127.0.0.1",))

    exchange = await web.request(WebRequest(method="GET", url="http://127.0.0.1:5432/"))

    assert seen == [] and exchange.refused is not None


async def test_redirects_are_returned_not_followed() -> None:
    async def redirect(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": "http://169.254.169.254/"})

    web, _, seen = client(redirect)

    exchange = await web.request(WebRequest(method="GET", url="http://example.com/"))

    assert len(seen) == 1
    assert exchange.status == 302
    assert ("location", "http://169.254.169.254/") in exchange.response_headers


async def test_a_long_body_is_shown_in_part_and_read_up_to_the_cap() -> None:
    web, blobs, _ = client(ok(b"x" * 100), limits=WebLimits(inline_bytes=10, body_cap=40))

    exchange = await web.request(WebRequest(method="GET", url="http://example.com/"))

    assert exchange.body == "x" * 10 and exchange.body_bytes == 10
    assert exchange.response_body_size == 40 and exchange.response_body_capped
    assert exchange.response_body_sha256 is not None
    assert blobs.blobs[exchange.response_body_sha256] == b"x" * 40


async def test_body_text_uses_the_declared_charset_and_drops_nul() -> None:
    body = "价格\x00".encode("gbk")
    web, _, _ = client(ok(body, headers={"content-type": "text/html; charset=gbk"}))

    exchange = await web.request(WebRequest(method="GET", url="http://example.com/"))

    assert exchange.body == "价格�"


async def test_failures_are_reported_in_the_exchange() -> None:
    async def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection reset", request=request)

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200)

    async def no_such_host(host: str, port: int) -> list[str]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    web, _, _ = client(unreachable)
    exchange = await web.request(WebRequest(method="GET", url="http://example.com/"))
    assert exchange.error is not None and "ConnectError" in exchange.error

    web, _, _ = client(slow)
    exchange = await web.request(
        WebRequest(method="GET", url="http://example.com/", timeout_s=0.05)
    )
    assert exchange.error == "timed out after 0.05 s"

    web = HttpWebClient(FakeBlobs(), resolve=no_such_host, transport=httpx2.MockTransport(ok()))
    exchange = await web.request(WebRequest(method="GET", url="http://nowhere.example/"))
    assert exchange.error is not None and "could not resolve nowhere.example" in exchange.error


async def test_only_http_urls_and_bounded_bodies_are_sent() -> None:
    web, _, seen = client(ok(), limits=WebLimits(request_body_cap=4))

    for url in ["file:///etc/passwd", "ftp://example.com/", "/relative", "http://"]:
        exchange = await web.request(WebRequest(method="GET", url=url))
        assert exchange.error is not None, url
    exchange = await web.request(WebRequest(method="POST", url="http://example.com/", body="12345"))
    assert exchange.error == "the request body is 5 bytes; the limit is 4"
    assert seen == []


async def test_cookies_a_response_sets_are_never_sent_back() -> None:
    async def set_cookie(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"set-cookie": "session=abc; Path=/"})

    web, _, seen = client(set_cookie)

    await web.request(WebRequest(method="GET", url="http://example.com/login"))
    await web.request(WebRequest(method="GET", url="http://other.example/"))

    assert [r.headers.get("cookie") for r in seen] == [None, None]
