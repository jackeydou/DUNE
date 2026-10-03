"""The `scan` job: a rule set over runs' event payloads, after decoding, stored in
`analysis.rule_matches` (docs/services/analysis.md#capabilities).

Every string in an event's payload is searched on its own, as is and through the decodings
`swarmeval.honeypot.decode.views` finds (base64, hex, gzip, zlib, chained). Keywords are also
found under a single-byte XOR. Each rule keeps its best match per event: the one with the fewest
decodings, then the first field. A scan is stored per rule set hash and run, replacing what an
earlier scan of the same pair stored.
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from pydantic import JsonValue
from sqlalchemy import delete, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.db import rule_matches, rule_scans
from swarmeval.detect.rules import RuleSet
from swarmeval.detect.search import search


@dataclass(frozen=True)
class Match:
    run_id: str
    event_id: str
    seq: int
    rule_id: str
    field: str
    """Where in the payload, e.g. `output.choices[0].message.content[1].text`."""
    via: tuple[str, ...]
    excerpt: str


def scan_events(run_id: str, rows: Sequence[Mapping[str, object]], rules: RuleSet) -> list[Match]:
    """`rows` are a run's `events.parquet` rows."""
    compiled = [(rule, rule.pattern()) for rule in rules.rules]
    matches: list[Match] = []
    for row in rows:
        seq = row["seq"]
        assert isinstance(seq, int)
        best: dict[str, Match] = {}
        for field, text in _strings(json.loads(str(row["payload"]))):
            for rule_id, via, excerpt in search(text, compiled):
                current = best.get(rule_id)
                if current is None or len(via) < len(current.via):
                    best[rule_id] = Match(
                        run_id, str(row["event_id"]), seq, rule_id, field, via, excerpt
                    )
        matches.extend(best[rule.id] for rule in rules.rules if rule.id in best)
    return matches


def _strings(value: JsonValue, path: str = "") -> Iterator[tuple[str, str]]:
    """Every string in a JSON value, with its path, in document order."""
    match value:
        case str():
            yield path, value
        case dict():
            for key, item in value.items():
                yield from _strings(item, f"{path}.{key}" if path else key)
        case list():
            for i, item in enumerate(value):
                yield from _strings(item, f"{path}[{i}]")
        case _:
            pass


async def store_scan(
    engine: AsyncEngine, run_id: str, rules: RuleSet, matches: Sequence[Match]
) -> None:
    """Replaces whatever an earlier scan of this rule set stored for this run, in one
    transaction."""
    digest = rules.sha256()
    async with engine.begin() as conn:
        await conn.execute(
            delete(rule_matches).where(
                rule_matches.c.rule_set_sha256 == digest, rule_matches.c.run_id == run_id
            )
        )
        upsert = pg_insert(rule_scans).values(
            rule_set_sha256=digest,
            run_id=run_id,
            rules=rules.model_dump(mode="json"),
            matches=len(matches),
        )
        await conn.execute(
            upsert.on_conflict_do_update(
                index_elements=[rule_scans.c.rule_set_sha256, rule_scans.c.run_id],
                set_={"matches": upsert.excluded.matches, "scanned_at": upsert.excluded.scanned_at},
            )
        )
        if matches:
            await conn.execute(
                insert(rule_matches),
                [
                    {
                        "rule_set_sha256": digest,
                        "run_id": m.run_id,
                        "event_id": m.event_id,
                        "rule_id": m.rule_id,
                        "seq": m.seq,
                        "field": m.field,
                        "via": list(m.via),
                        "excerpt": m.excerpt,
                    }
                    for m in matches
                ],
            )
