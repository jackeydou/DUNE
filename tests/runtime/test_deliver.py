"""`before_deliver`: per message and recipient, chained, with every change an intervention on the
send, and the verdicts kept in `runs.deliveries` (M2 spec decision 2)."""

import pytest
from pydantic import BaseModel

from swarmeval.gateway.bus import ChannelSpec
from swarmeval.runtime.extensions import (
    Delay,
    Deliver,
    DeliveryDecision,
    Drop,
    Envelope,
    ExtensionAPI,
    ExtensionError,
    ExtensionUse,
    HookContext,
    NoConfig,
    NoState,
    UserMessage,
    extension,
)
from swarmeval.runtime.records import (
    ExtensionSnapshot,
    InterventionRecord,
    LifecycleRecord,
    MessageDeliverRecord,
)
from tests.runtime.fakes import FakeStore, Harness, agent, call, harness, reply
from tests.runtime.test_causal import assert_rooted

TEAM = (ChannelSpec(id="team", members=("dev", "qa")),)
SEND = call("send_message", '{"channel": "team", "content": "ship it"}')
LS = call("shell", '{"cmd": "ls"}')
seen: list[Envelope] = []


@extension(id="t.upper", api_version=1)
def upper(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_deliver")
    async def _(ctx: HookContext[NoState], envelope: Envelope) -> DeliveryDecision:
        seen.append(envelope)
        return Deliver(content=envelope.content.upper())


@extension(id="t.sign", api_version=1)
def sign(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_deliver")
    async def _(ctx: HookContext[NoState], envelope: Envelope) -> DeliveryDecision:
        seen.append(envelope)
        return Deliver(content=envelope.content + " -- bus")


@extension(id="t.same", api_version=1)
def same(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_deliver")
    async def _(ctx: HookContext[NoState], envelope: Envelope) -> DeliveryDecision:
        return Deliver(content=envelope.content)


@extension(id="t.drop", api_version=1)
def drop(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_deliver")
    async def _(ctx: HookContext[NoState], envelope: Envelope) -> DeliveryDecision:
        return Drop(reason="lost")


class Turns(BaseModel):
    turns: int = 1


@extension(id="t.hold", api_version=1, config=Turns)
def hold(ext: ExtensionAPI[Turns, NoState]) -> None:
    @ext.on("before_deliver")
    async def _(ctx: HookContext[NoState], envelope: Envelope) -> DeliveryDecision:
        seen.append(envelope)
        return Delay(turns=ext.config.turns)


@extension(id="t.bad", api_version=1)
def bad(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_deliver")
    async def _(ctx: HookContext[NoState], envelope: Envelope) -> DeliveryDecision:
        return envelope.content  # pyright: ignore[reportReturnType]


def team(qa_replies: int = 1, dev_after: int = 1, **kwargs: object) -> Harness:
    return harness(
        (agent("dev", tools=("send_message",)), agent("qa", tools=())),
        {
            "dev": [reply("", SEND), *[reply("done")] * dev_after],
            "qa": [reply(f"qa {i}") for i in range(qa_replies)],
        },
        channels=TEAM,
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


def qa_users(h: Harness) -> list[str]:
    return [m.content for m in h.store.messages("qa")[2:] if isinstance(m, UserMessage)]


async def test_rewrites_chain_in_load_order_and_each_is_an_intervention_on_the_send() -> None:
    seen.clear()
    h = team(qa_replies=2, extensions=[upper, same, sign])

    await h.loop.run()

    assert qa_users(h) == ["Message from dev on channel `team`:\n\nSHIP IT -- bus"]
    assert [e.content for e in seen] == ["ship it", "SHIP IT"]
    (send,) = h.store.records("msg.send")
    assert seen[0].send_event_id == send.event_id and seen[0].recipient == "qa"
    rewrites = h.store.records("intervention")
    assert [(e.extension, e.agent_id, e.parent_id) for e in rewrites] == [
        ("t.upper", "qa", send.event_id),
        ("t.sign", "qa", send.event_id),
    ]
    first = rewrites[0].record
    assert isinstance(first, InterventionRecord)
    assert (first.hook, first.action, first.target_event_id) == (
        "before_deliver",
        "deliver",
        send.event_id,
    )
    assert first.after == {"recipient": "qa", "kind": "deliver", "content": "SHIP IT"}
    (deliver,) = h.store.records("msg.deliver")
    assert isinstance(deliver.record, MessageDeliverRecord)
    assert deliver.record.content == "SHIP IT -- bus"
    assert deliver.parent_id == rewrites[-1].event_id
    assert h.store.deliveries == {(send.seq, "qa"): ("delivered", None)}
    assert_rooted(h.store.events)


async def test_a_drop_ends_the_chain_and_the_message_never_arrives() -> None:
    seen.clear()
    h = team(qa_replies=1, extensions=[drop, upper])

    outcome = await h.loop.run()

    assert outcome.status == "finished"
    assert seen == []
    assert qa_users(h) == []
    assert h.store.records("msg.deliver") == []
    (send,) = h.store.records("msg.send")
    (dropped,) = h.store.records("intervention")
    assert dropped.record.model_dump()["after"] == {
        "recipient": "qa",
        "kind": "drop",
        "reason": "lost",
    }
    assert dropped.parent_id == send.event_id
    assert h.store.deliveries == {(send.seq, "qa"): ("dropped", None)}


async def test_a_delay_counts_the_recipients_own_turns() -> None:
    seen.clear()
    use = (hold, ExtensionUse(use="t.hold", config={"turns": 2}))
    # dev sends in its turn 2, after qa's turn 1: held for qa's turns 2 and 3, it arrives at the
    # start of qa's turn 4.
    h = harness(
        (agent("dev", tools=("send_message", "shell")), agent("qa", tools=("shell",))),
        {
            "dev": [reply("", LS), reply("", SEND), reply("done")],
            "qa": [reply("", LS)] * 3 + [reply("got it")],
        },
        channels=TEAM,
        extensions=[use],
    )

    await h.loop.run()

    (send,) = h.store.records("msg.send")
    assert h.store.deliveries[(send.seq, "qa")] == ("delivered", 4)
    (deliver,) = h.store.records("msg.deliver")
    qa_turn_at_delivery = next(
        r.turn
        for txn in h.store.log
        if any(e.event_id == deliver.event_id for e in txn.events)
        for r in txn.agent_states
        if r.agent_id == "qa"
    )
    assert qa_turn_at_delivery == 4
    (held,) = h.store.records("intervention")
    assert held.record.model_dump()["after"] == {"recipient": "qa", "kind": "delay", "turns": 2}
    assert deliver.parent_id == held.event_id


async def test_a_message_still_held_at_run_end_is_never_delivered() -> None:
    use = (hold, ExtensionUse(use="t.hold", config={"turns": 3}))
    h = team(qa_replies=1, extensions=[use])

    outcome = await h.loop.run()

    assert outcome.status == "finished"
    (send,) = h.store.records("msg.send")
    assert h.store.deliveries == {(send.seq, "qa"): ("delayed", 4)}
    assert h.store.records("msg.deliver") == []
    assert qa_users(h) == []


async def test_a_held_message_wakes_a_finished_agent_once_it_is_due() -> None:
    seen.clear()
    use = (hold, ExtensionUse(use="t.hold", config={"turns": 1}))
    # qa finishes at its turn 1, before dev sends in its turn 2. Held for 1 of qa's turns, the
    # message is due at qa's turn 3; qa, finished, takes no turn 2, so it stays held.
    late = harness(
        (agent("dev", tools=("send_message", "shell")), agent("qa", tools=())),
        {"dev": [reply("", LS), reply("", SEND), reply("done")], "qa": [reply("idle")]},
        channels=TEAM,
        extensions=[use],
    )
    await late.loop.run()
    (send,) = late.store.records("msg.send")
    assert late.store.deliveries == {(send.seq, "qa"): ("delayed", 3)}

    # Sent before qa's first turn, due at qa's turn 2: qa finishes at turn 1 and is woken for it.
    early = team(qa_replies=2, extensions=[use])
    await early.loop.run()
    (send,) = early.store.records("msg.send")
    assert early.store.deliveries[(send.seq, "qa")] == ("delivered", 2)
    assert len(qa_users(early)) == 1


async def test_delays_add_up_and_later_handlers_still_run() -> None:
    seen.clear()
    first = (
        hold,
        ExtensionUse.model_validate({"use": "t.hold", "as": "h1", "config": {"turns": 1}}),
    )
    second = (
        hold,
        ExtensionUse.model_validate({"use": "t.hold", "as": "h2", "config": {"turns": 2}}),
    )
    h = team(qa_replies=1, extensions=[first, second, upper])

    await h.loop.run()

    assert [e.delayed_turns for e in seen] == [0, 1, 3]
    (send,) = h.store.records("msg.send")
    assert h.store.deliveries[(send.seq, "qa")] == ("delayed", 4)


async def test_a_verdict_of_the_wrong_type_fails_the_run() -> None:
    h = team(extensions=[bad])

    with pytest.raises(ExtensionError, match="return Deliver, Drop, or Delay"):
        await h.loop.run()

    end = h.store.events[-1].record
    assert isinstance(end, LifecycleRecord) and end.status == "failed"


@extension(id="t.dice2", api_version=1)
def dice(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_run_start")
    async def _(ctx: HookContext[NoState]) -> None:
        rolls.append(ctx.rng.random())

    @ext.on("on_run_end")
    async def _end(ctx: HookContext[NoState]) -> None:
        rolls.append(ctx.rng.random())


rolls: list[float] = []


async def test_the_random_stream_continues_from_the_stored_count() -> None:
    rolls.clear()
    fresh = harness((agent(),), {"a": [reply("done")]}, extensions=[dice])
    await fresh.loop.run()
    assert [v.rng_uses for i, _, v in fresh.store.extension_rows if i == "t.dice2"] == [1, 2]

    resumed = harness(
        (agent(),),
        {"a": [reply("done")]},
        extensions=[dice],
        store=FakeStore(extension_rows=[("t.dice2", 0, ExtensionSnapshot({}, rng_uses=1))]),
    )
    await resumed.loop.run()

    assert rolls[2] == rolls[1] != rolls[0]
