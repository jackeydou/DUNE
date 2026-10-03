import asyncio

import psycopg
import pytest
from inspect_ai.event import Event
from pydantic import TypeAdapter
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.db import agent_state, control_runs, deliveries, events, messages
from swarmeval.events import (
    NOTIFY_CHANNEL,
    ChainRow,
    FencedError,
    PostgresRunStore,
    RunNotFoundError,
    verify,
)
from swarmeval.runtime.loop import RunLoop, RunSpec
from swarmeval.runtime.messages import SystemMessage, UserMessage
from swarmeval.runtime.records import (
    AgentStateRow,
    AlertRecord,
    DeliveryChange,
    EventDraft,
    ExecResult,
    ExtensionSnapshot,
    FsChange,
    MessageDeliverRecord,
    MessageSendRecord,
    Transaction,
)
from swarmeval.runtime.writer import RunWriter
from tests.runtime.fakes import SHELL, FakeSandbox, FakeStore, ScriptedModel, agent, call, reply

pytestmark = pytest.mark.docker

EVENT = TypeAdapter[Event](Event)


def store_for(engine: AsyncEngine, run_id: str, *, owner_epoch: int = 1) -> PostgresRunStore:
    return PostgresRunStore(
        engine,
        run_id=run_id,
        workspace="ws_test",
        owner_epoch=owner_epoch,
        sandboxes={"a": "box_a", "b": "box_b"},
    )


async def chain_rows(engine: AsyncEngine, run_id: str) -> list[ChainRow]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(events.c.seq, events.c.prev_hash, events.c.hash, events.c.payload)
            .where(events.c.run_id == run_id)
            .order_by(events.c.seq)
        )
        return [ChainRow(seq=s, prev_hash=p, hash=h, payload=j) for s, p, h, j in rows]


def start(agent_id: str = "a") -> Transaction:
    return Transaction(
        new_generations={agent_id: (SystemMessage(content="sys"), UserMessage(content="task"))},
        agent_states=[
            AgentStateRow(agent_id, gen=0, length=2, turn=0, status="ready", tokens_used=0)
        ],
    )


def alert(message: str) -> EventDraft:
    return EventDraft(record=AlertRecord(message=message, severity="low"))


async def run_loop(store: FakeStore | PostgresRunStore, run_id: str) -> FakeSandbox:
    writer = RunWriter(store)
    write = ExecResult(
        exit_code=0,
        stdout="ok",
        stderr="",
        fs_changes=(
            FsChange(
                path="/workspace/x", op="create", uid=0, before_sha256=None, after_sha256="ab"
            ),
        ),
    )
    sandbox = FakeSandbox(handler=lambda _: write)
    scripts = {
        "a": [reply("", call("shell", '{"cmd": "touch x"}')), reply("done")],
        "b": [reply("nothing to do")],
    }
    loop = RunLoop(
        RunSpec(run_id=run_id, seed=1, agents=(agent("a"), agent("b"))),
        writer=writer,
        model_client=ScriptedModel(writer, scripts),
        sandbox_executor=sandbox,
        tools=[SHELL],
    )
    await loop.run()
    return sandbox


async def test_a_run_stores_the_same_history_as_the_in_memory_store(
    engine: AsyncEngine, run_id: str
) -> None:
    store = store_for(engine, run_id)
    fake = FakeStore()

    await run_loop(store, run_id)
    await run_loop(fake, "fake")

    rows = await chain_rows(engine, run_id)
    assert verify(run_id, rows) == len(fake.events)
    stored = [EVENT.validate_python(r.payload) for r in rows]
    kinds = [e.metadata["swarmeval"]["source"] for e in stored if e.metadata is not None]
    assert len(kinds) == len(stored)
    types = [e.event for e in stored]
    assert types == ["info", "model", "tool", "model", "model", "info"]
    for agent_id in ("a", "b"):
        context = await store.context(agent_id)
        assert context is not None
        assert list(context.messages) == fake.messages(agent_id)
    async with engine.connect() as conn:
        tool = (
            await conn.execute(
                select(events.c.sandbox_id, events.c.parent_id, events.c.type).where(
                    events.c.run_id == run_id, events.c.seq == 3
                )
            )
        ).one()
        model_id = (
            await conn.execute(
                select(events.c.event_id).where(events.c.run_id == run_id, events.c.seq == 2)
            )
        ).scalar_one()
    assert tuple(tool) == ("box_a", model_id, "tool")


async def test_every_event_row_matches_its_payload(engine: AsyncEngine, run_id: str) -> None:
    await run_loop(store_for(engine, run_id), run_id)

    async with engine.connect() as conn:
        rows = await conn.execute(
            select(events.c.seq, events.c.event_id, events.c.agent_id, events.c.payload).where(
                events.c.run_id == run_id
            )
        )
        for seq, event_id, agent_id, payload in rows:
            ours = payload["metadata"]["swarmeval"]
            assert (payload["uuid"], ours["seq"], ours["agent_id"]) == (event_id, seq, agent_id)
            assert ours["workspace"] == "ws_test"


async def test_a_stale_owner_cannot_write(engine: AsyncEngine, run_id: str) -> None:
    store = store_for(engine, run_id)
    await store.commit(Transaction(events=[alert("first")]))
    async with engine.begin() as conn:
        await conn.execute(
            update(control_runs).where(control_runs.c.run_id == run_id).values(owner_epoch=2)
        )

    with pytest.raises(FencedError, match="owner_epoch 2, this worker holds 1"):
        txn = start()
        txn.events.append(alert("second"))
        await store.commit(txn)

    assert len(await chain_rows(engine, run_id)) == 1
    async with engine.connect() as conn:
        written = await conn.execute(
            select(func.count()).select_from(messages).where(messages.c.run_id == run_id)
        )
        assert written.scalar_one() == 0


async def test_a_run_without_a_control_row_is_refused(engine: AsyncEngine) -> None:
    store = store_for(engine, "run_missing")

    with pytest.raises(RunNotFoundError, match="run_missing"):
        await store.commit(Transaction(events=[alert("x")]))


async def test_a_new_store_continues_the_chain_and_the_context(
    engine: AsyncEngine, run_id: str
) -> None:
    first = store_for(engine, run_id)
    await first.commit(start())
    await first.commit(Transaction(events=[alert("one")]))

    second = store_for(engine, run_id)
    await second.commit(
        Transaction(
            events=[alert("two")],
            messages=[("a", UserMessage(content="more"))],
            agent_states=[
                AgentStateRow("a", gen=0, length=3, turn=1, status="ready", tokens_used=0)
            ],
        )
    )

    assert verify(run_id, await chain_rows(engine, run_id)) == 2
    context = await second.context("a")
    assert context is not None
    assert [m.role for m in context.messages] == ["system", "user", "user"]


async def test_a_new_generation_keeps_the_old_one(engine: AsyncEngine, run_id: str) -> None:
    store = store_for(engine, run_id)
    await store.commit(start())
    await store.commit(
        Transaction(
            new_generations={"a": (SystemMessage(content="compacted"),)},
            agent_states=[
                AgentStateRow("a", gen=1, length=1, turn=1, status="ready", tokens_used=0)
            ],
        )
    )

    context = await store.context("a")
    assert context is not None
    assert (context.gen, context.messages) == (1, (SystemMessage(content="compacted"),))
    async with engine.connect() as conn:
        gens = await conn.execute(
            select(messages.c.gen, func.count())
            .where(messages.c.run_id == run_id)
            .group_by(messages.c.gen)
            .order_by(messages.c.gen)
        )
        assert [tuple(r) for r in gens] == [(0, 2), (1, 1)]


async def test_context_reads_the_state_as_of_its_latest_row(
    engine: AsyncEngine, run_id: str
) -> None:
    store = store_for(engine, run_id)
    await store.commit(start())
    await store.commit(Transaction(messages=[("a", UserMessage(content="not admitted yet"))]))

    context = await store.context("a")

    assert context is not None
    assert len(context.messages) == 2
    async with engine.connect() as conn:
        seqs = await conn.execute(select(agent_state.c.seq).where(agent_state.c.run_id == run_id))
        assert list(seqs.scalars()) == [0]


async def test_the_latest_extension_state_wins(engine: AsyncEngine, run_id: str) -> None:
    store = store_for(engine, run_id)
    await store.commit(
        Transaction(
            extension_states={"x": ExtensionSnapshot({"n": 1}), "y": ExtensionSnapshot([1])}
        )
    )
    await store.commit(
        Transaction(events=[alert("a")], extension_states={"x": ExtensionSnapshot({"n": 2}, 3)})
    )

    assert await store.extension_states() == {
        "x": ExtensionSnapshot({"n": 2}, 3),
        "y": ExtensionSnapshot([1]),
    }


async def test_a_commit_with_events_notifies_listeners(
    postgres_url: str, engine: AsyncEngine, run_id: str
) -> None:
    store = store_for(engine, run_id)
    async with await psycopg.AsyncConnection.connect(postgres_url, autocommit=True) as listener:
        await listener.execute(f"LISTEN {NOTIFY_CHANNEL}")
        await store.commit(Transaction(events=[alert("x")]))

        notify = await asyncio.wait_for(anext(listener.notifies()), timeout=5)

    assert (notify.channel, notify.payload) == (NOTIFY_CHANNEL, run_id)


async def test_a_send_opens_a_delivery_row_that_its_delivery_closes(
    engine: AsyncEngine, run_id: str
) -> None:
    store = store_for(engine, run_id)
    send = MessageSendRecord(
        channel="team", sender="a", content="hi", recipients=("b",), call_id="c1"
    )
    (sent,) = await store.commit(Transaction(events=[EventDraft(send, agent_id="a")]))

    async def rows() -> list[tuple[int, str, str, int | None]]:
        async with engine.connect() as conn:
            result = await conn.execute(
                select(
                    deliveries.c.msg_seq,
                    deliveries.c.recipient,
                    deliveries.c.status,
                    deliveries.c.delivered_seq,
                ).where(deliveries.c.run_id == run_id)
            )
            return [tuple(r) for r in result]  # pyright: ignore[reportReturnType]

    assert await rows() == [(sent.seq, "b", "pending", None)]
    deliver = MessageDeliverRecord(
        channel="team", sender="a", recipient="b", send_seq=sent.seq, content="hi"
    )
    (delivered,) = await store.commit(Transaction(events=[EventDraft(deliver, agent_id="b")]))
    assert await rows() == [(sent.seq, "b", "delivered", delivered.seq)]

    with pytest.raises(RuntimeError, match="has no pending or delayed row"):
        await store.commit(Transaction(events=[EventDraft(deliver, agent_id="b")]))
    assert await rows() == [(sent.seq, "b", "delivered", delivered.seq)]


async def test_before_deliver_verdicts_move_delivery_rows(engine: AsyncEngine, run_id: str) -> None:
    store = store_for(engine, run_id)
    send = MessageSendRecord(
        channel="team", sender="a", content="hi", recipients=("b", "c", "d"), call_id="c1"
    )
    (sent,) = await store.commit(Transaction(events=[EventDraft(send, agent_id="a")]))
    await store.commit(
        Transaction(
            deliveries=[
                DeliveryChange(sent.seq, "b", "dropped"),
                DeliveryChange(sent.seq, "c", "delayed", due_turn=3),
            ]
        )
    )

    async def rows() -> dict[str, tuple[str, int | None, int | None]]:
        async with engine.connect() as conn:
            result = await conn.execute(
                select(
                    deliveries.c.recipient,
                    deliveries.c.status,
                    deliveries.c.due_turn,
                    deliveries.c.delivered_seq,
                ).where(deliveries.c.run_id == run_id)
            )
            return {r[0]: (r[1], r[2], r[3]) for r in result}

    assert await rows() == {
        "b": ("dropped", None, None),
        "c": ("delayed", 3, None),
        "d": ("pending", None, None),
    }

    def deliver(recipient: str) -> Transaction:
        record = MessageDeliverRecord(
            channel="team", sender="a", recipient=recipient, send_seq=sent.seq, content="hi"
        )
        return Transaction(events=[EventDraft(record, agent_id=recipient)])

    (delivered,) = await store.commit(deliver("c"))
    assert (await rows())["c"] == ("delivered", 3, delivered.seq)
    # A dropped message is never delivered, and a verdict applies only to a pending row.
    with pytest.raises(RuntimeError, match="never after it was dropped"):
        await store.commit(deliver("b"))
    with pytest.raises(RuntimeError, match="cannot become `delayed`: it has no pending row"):
        await store.commit(Transaction(deliveries=[DeliveryChange(sent.seq, "b", "delayed", 1)]))
    assert (await rows())["b"] == ("dropped", None, None)
