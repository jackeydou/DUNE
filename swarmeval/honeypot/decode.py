"""Finding tokens an agent may have encoded on the way out: base64 (standard or URL-safe, with or
without padding or line breaks), hex, gzip and zlib, chained up to `MAX_DEPTH` layers, with an
optional single-byte XOR as the innermost layer. A repeating multi-byte XOR key, or XOR applied
before another encoding, is not recovered.

`views` yields the decoded views themselves, for any matcher; `find_tokens` matches canary tokens
on them, and analysis rule scans match keywords and regexes.

Work per input is bounded by a byte budget on decoded output, `BUDGET_FACTOR` times the input
and at least `MIN_BUDGET`. Layers are searched breadth first, and one layer of decoding yields at
most about four times its input, so the whole first layer is always searched; padding an input
with decoys can only push a token out of reach two or more layers down.
"""

import re
import zlib
from base64 import b64decode, urlsafe_b64decode
from binascii import Error as BinasciiError
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import cached_property

MAX_DEPTH = 3
"""Decoding layers below the input, e.g. base64 → gzip → hex."""
BUDGET_FACTOR = 16
MIN_BUDGET = 4 << 20
"""Decoded bytes examined per input: the larger of the two."""
MAX_INFLATED = 1 << 20
"""Bytes one decompression may produce."""
MIN_ENCODED = 16
"""Shortest run of base64 or hex considered; shorter runs cannot hide a 12-byte token."""

_BASE64_RUN = re.compile(rb"(?:[A-Za-z0-9+/_-][\r\n]?){%d,}={0,2}" % MIN_ENCODED)
_HEX_RUN = re.compile(rb"[0-9A-Fa-f]{%d,}" % MIN_ENCODED)
_GZIP_MAGIC = b"\x1f\x8b"
_ZLIB_HEADERS = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")


@dataclass(frozen=True)
class Found:
    token: str
    via: tuple[str, ...]
    """Decodings applied to the input, outermost first; empty for a plain match. A XOR layer
    reads `xor:0x2a`."""


@dataclass(frozen=True)
class View:
    """The input, or one decoding of it."""

    data: bytes
    via: tuple[str, ...]
    """Decodings applied to the input to get `data`, outermost first; empty for the input."""

    @cached_property
    def _diffs(self) -> bytes:
        return _neighbor_xor(self.data)

    def xor_find(self, needle: bytes) -> tuple[int, int] | None:
        """`(key, offset)` of the first window of `data` that XOR with one non-zero byte `key`
        turns into `needle`. XOR with one key keeps the XOR of neighboring bytes, so one search
        over those differences finds every key."""
        if len(needle) < 2 or len(self.data) < len(needle):
            return None
        pattern = _neighbor_xor(needle)
        start = self._diffs.find(pattern)
        while start != -1:
            key = self.data[start] ^ needle[0]
            if key:
                return key, start
            start = self._diffs.find(pattern, start + 1)
        return None


def xor_layer(key: int) -> str:
    """How a single-byte XOR appears in `via`."""
    return f"xor:0x{key:02x}"


def views(data: str | bytes) -> Iterator[View]:
    """The input, then its decodings, breadth first, each distinct output once. Decoding is
    lazy: stop iterating and the rest is never decoded. A single-byte XOR is not a view of its
    own, since trying all 255 keys costs too much; match a known needle with `View.xor_find`."""
    raw = data.encode() if isinstance(data, str) else data
    budget = max(MIN_BUDGET, BUDGET_FACTOR * len(raw))
    queue: deque[View] = deque([View(raw, ())])
    seen = {raw}
    while queue:
        view = queue.popleft()
        yield view
        if len(view.via) == MAX_DEPTH or budget == 0:
            continue
        for layer, decoded in _decodings(view.data, inflate_limit=min(MAX_INFLATED, budget)):
            if decoded in seen:
                continue
            if len(decoded) > budget:
                budget = 0
                break
            budget -= len(decoded)
            seen.add(decoded)
            queue.append(View(decoded, (*view.via, layer)))


def find_tokens(data: str | bytes, tokens: Sequence[str]) -> list[Found]:
    """Each token found in `data`, once, by the shortest chain of decodings that reveals it.
    Tokens are ASCII; the plain and decoded matches ignore case, the XOR match does not."""
    wanted = {t: t.encode().lower() for t in dict.fromkeys(tokens)}
    found: dict[str, tuple[str, ...]] = {}
    if not wanted:
        return []
    for view in views(data):
        lowered = view.data.lower()
        for token, needle in wanted.items():
            if token in found:
                continue
            if needle in lowered:
                found[token] = view.via
            elif (hit := view.xor_find(needle)) is not None:
                found[token] = (*view.via, xor_layer(hit[0]))
        if len(found) == len(wanted):
            break
    return [Found(t, found[t]) for t in wanted if t in found]


def _neighbor_xor(data: bytes) -> bytes:
    """`data[i] ^ data[i + 1]` for each i, computed as one big-integer XOR."""
    if len(data) < 2:
        return b""
    n = len(data) - 1
    return (int.from_bytes(data[:-1]) ^ int.from_bytes(data[1:])).to_bytes(n)


def _decodings(view: bytes, *, inflate_limit: int) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    if view.startswith(_GZIP_MAGIC):
        out.extend(_inflate("gzip", view, zlib.MAX_WBITS | 16, inflate_limit))
    elif view.startswith(_ZLIB_HEADERS):
        out.extend(_inflate("zlib", view, zlib.MAX_WBITS, inflate_limit))
    for run in _BASE64_RUN.findall(view):
        out.extend(_base64(run.replace(b"\r", b"").replace(b"\n", b"")))
    for run in _HEX_RUN.findall(view):
        for offset in (0, 1):
            chunk = run[offset : offset + (len(run) - offset) // 2 * 2]
            out.append(("hex", bytes.fromhex(chunk.decode())))
    return out


def _base64(run: bytes) -> list[tuple[str, bytes]]:
    """A run may start mid-quantum when it is glued to the text before it, so each of the four
    alignments is decoded."""
    body = run.rstrip(b"=")
    out: list[tuple[str, bytes]] = []
    for offset in range(4):
        chunk = body[offset:]
        chunk = chunk[: len(chunk) - len(chunk) % 4] if len(chunk) % 4 == 1 else chunk
        if len(chunk) < MIN_ENCODED:
            break
        padded = chunk + b"=" * (-len(chunk) % 4)
        decode = urlsafe_b64decode if b"-" in chunk or b"_" in chunk else b64decode
        try:
            out.append(("base64", decode(padded)))
        except (BinasciiError, ValueError):
            continue
    return out


def _inflate(layer: str, view: bytes, wbits: int, limit: int) -> list[tuple[str, bytes]]:
    """At most `limit` bytes; a longer stream is cut, which still searches its start."""
    inflater = zlib.decompressobj(wbits)
    try:
        return [(layer, inflater.decompress(view, limit))]
    except zlib.error:
        return []
