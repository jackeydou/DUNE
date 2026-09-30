import hashlib

import pytest
from pydantic import JsonValue

from swarmeval.events import ChainError, ChainRow, genesis, link, verify


def chain(run_id: str, payloads: list[JsonValue]) -> list[ChainRow]:
    rows: list[ChainRow] = []
    prev = genesis(run_id)
    for seq, payload in enumerate(payloads, start=1):
        digest = link(prev, seq, payload)
        rows.append(ChainRow(seq=seq, prev_hash=prev, hash=digest, payload=payload))
        prev = digest
    return rows


def test_genesis_and_link_follow_the_documented_formula() -> None:
    first = hashlib.sha256(b"swarmeval:run_1").digest()
    assert genesis("run_1") == first
    expected = hashlib.sha256(first + (1).to_bytes(8, "big") + b'{"a":1,"b":[true,null]}')
    assert link(first, 1, {"b": [True, None], "a": 1}) == expected.digest()


def test_key_order_does_not_change_the_hash() -> None:
    assert link(b"x", 3, {"a": 1, "b": 2}) == link(b"x", 3, {"b": 2, "a": 1})


def test_an_intact_chain_verifies() -> None:
    assert verify("run_1", chain("run_1", [{"n": 1}, {"n": 2}, {"n": 3}])) == 3


def test_an_edited_payload_is_detected() -> None:
    rows = chain("run_1", [{"n": 1}, {"n": 2}])
    rows[1] = ChainRow(seq=2, prev_hash=rows[1].prev_hash, hash=rows[1].hash, payload={"n": 9})

    with pytest.raises(ChainError, match="seq 2 stores hash"):
        verify("run_1", rows)


def test_a_missing_event_is_detected() -> None:
    rows = chain("run_1", [{"n": 1}, {"n": 2}, {"n": 3}])

    with pytest.raises(ChainError, match="expected seq 2, found seq 3"):
        verify("run_1", [rows[0], rows[2]])


def test_rows_from_another_run_do_not_verify() -> None:
    with pytest.raises(ChainError, match="seq 1 has prev_hash"):
        verify("run_2", chain("run_1", [{"n": 1}]))
