"""The per-run hash chain over stored event payloads (docs/event-log.md#hash-chain).

    hash[seq] = sha256( prev_hash || uint64_be(seq) || JCS(payload) )
    prev_hash of the first event = sha256("swarmeval:" || run_id)

The hash covers the payload as stored, canonicalized with RFC 8785, so anyone holding the rows
can recompute it without our code.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

import rfc8785
from pydantic import JsonValue


class ChainError(Exception):
    """A stored chain does not verify."""


def genesis(run_id: str) -> bytes:
    return hashlib.sha256(f"swarmeval:{run_id}".encode()).digest()


def canonical(payload: JsonValue) -> bytes:
    """RFC 8785 bytes. Raises `rfc8785.CanonicalizationError` for NaN, infinities, and integers
    outside ±(2**53 - 1), which JSON cannot carry exactly."""
    return rfc8785.dumps(payload)


def link(prev_hash: bytes, seq: int, payload: JsonValue) -> bytes:
    return hashlib.sha256(prev_hash + seq.to_bytes(8, "big") + canonical(payload)).digest()


@dataclass(frozen=True)
class ChainRow:
    seq: int
    prev_hash: bytes
    hash: bytes
    payload: JsonValue


def verify(run_id: str, rows: Iterable[ChainRow]) -> int:
    """Checks a run's rows in `seq` order, starting at 1. Returns the number of rows checked."""
    expected_prev = genesis(run_id)
    count = 0
    for count, row in enumerate(rows, start=1):
        if row.seq != count:
            raise ChainError(f"run {run_id}: expected seq {count}, found seq {row.seq}.")
        if row.prev_hash != expected_prev:
            raise ChainError(
                f"run {run_id}: seq {row.seq} has prev_hash {row.prev_hash.hex()}, but the "
                f"previous event's hash is {expected_prev.hex()}."
            )
        actual = link(row.prev_hash, row.seq, row.payload)
        if row.hash != actual:
            raise ChainError(
                f"run {run_id}: seq {row.seq} stores hash {row.hash.hex()}, but its payload "
                f"hashes to {actual.hex()}. The payload or the hash was changed after commit."
            )
        expected_prev = row.hash
    return count
