"""Rule matching over the decoded views of a text: as is, and through the decodings
`swarmeval.honeypot.decode.views` finds (base64, hex, gzip, zlib, chained), keywords also under a
single-byte XOR. Shared by analysis rule scans and the `rule` detector."""

import re
from collections.abc import Iterator, Sequence

from swarmeval.detect.rules import Rule
from swarmeval.honeypot.decode import View, views, xor_layer

CONTEXT_CHARS = 40
"""Characters kept on each side of a match in its excerpt."""
MAX_MATCH_CHARS = 120
"""Characters of the match itself kept in the excerpt."""

_ESCAPES = str.maketrans(
    {
        "\\": "\\\\",
        "\n": "\\n",
        "\r": "\\r",
        **{
            chr(c): f"\\u{c:04x}"
            for c in (*range(0x20), 0x7F, 0x85, 0x2028, 0x2029)
            if c not in (0x09, 0x0A, 0x0D)
        },
    }
)


def one_line(text: str) -> str:
    """Escapes every line break: backslashes first, then control characters and the separators
    `str.splitlines` breaks on. Tabs stay."""
    return text.translate(_ESCAPES)


def search(
    text: str, rules: Sequence[tuple[Rule, re.Pattern[str]]]
) -> Iterator[tuple[str, tuple[str, ...], str]]:
    """Each rule's first match in `text`, by the shortest chain of decodings: the rule id, the
    decodings (`via`, outermost first), and an excerpt on one line."""
    left = dict.fromkeys(rule.id for rule, _ in rules)
    for view in views(text):
        decoded = view.data.decode("utf-8", errors="replace")
        for rule, pattern in rules:
            if rule.id not in left:
                continue
            found = pattern.search(decoded)
            if found is not None:
                del left[rule.id]
                yield rule.id, view.via, excerpt(decoded, found.start(), found.end())
            elif (hit := _xor(rule, view)) is not None:
                del left[rule.id]
                yield rule.id, (*view.via, xor_layer(hit[0])), hit[1]
        if not left:
            return


def _xor(rule: Rule, view: View) -> tuple[int, str] | None:
    """A keyword under a single-byte XOR, matched byte for byte, and its excerpt."""
    if rule.keyword is None or rule.ignore_case:
        return None
    needle = rule.keyword.encode()
    hit = view.xor_find(needle)
    if hit is None:
        return None
    key, start = hit
    lo = max(0, start - CONTEXT_CHARS)
    window = bytes(b ^ key for b in view.data[lo : start + len(needle) + CONTEXT_CHARS])
    text = window.decode("utf-8", errors="replace")
    return key, excerpt(text, start - lo, start - lo + len(rule.keyword))


def excerpt(text: str, start: int, end: int) -> str:
    """The match with some context, on one line; never holds NUL, which Postgres text
    refuses."""
    end = min(end, start + MAX_MATCH_CHARS)
    lo, hi = max(0, start - CONTEXT_CHARS), min(len(text), end + CONTEXT_CHARS)
    return ("…" if lo else "") + one_line(text[lo:hi]) + ("…" if hi < len(text) else "")
