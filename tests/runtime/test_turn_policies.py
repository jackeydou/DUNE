"""`event_driven` and `async` turn policies, and the `wall_clock` limit (M2 spec decision 10)."""

import asyncio
import json
from typing import Any

from swarmeval.gateway.bus import ChannelSpec
from swarmeval.gateway.bus.interventions import delay
from swarmeval.runtime.extensions import (
    ExtensionAPI,
    ExtensionUse,
    HookContext,
    NoConfig,
    NoState,
    UserMessage,
    extension,
)
from swarmeval.runtime.records import (
    CommittedEvent,
    LifecycleRecord,
    LimitRecord,
    MessageSendRecord,
    ModelCallRecord,
)
from swarmeval.runtime.specs import Limits
from tests.runtime.fakes import FakeStore, agent, call, harness, reply

AB = (ChannelSpec(id="ab", members=("a", "b")),)


def send(content: str, id: str) -> Any:
    return call("send_message", json.dumps({"channel": "ab", "content": content}), id=id)


def callers(store: FakeStore) -> list[str]:
    return [e.agent_id or "-" for e in store.events if isinstance(e.record, ModelCallRecord)]


def users(store: FakeStore, agent_id: str) -> list[str]:
    return [m.content for m in store.messages(agent_id)[2:] if isinstance(m, UserMessage)]


async def test_event_driven_agents_work_through_their_step_before_the_next_goes() -> None:
    h = harness(
        (agent("a", tools=("shell", "send_message")), agent("b", tools=("send_message",))),
        {
            "a": [
                reply("", call("shell", '{"cmd": "ls"}', id="l1")),
                reply("", send("hi", "s")),
                reply("done"),
            ],
            "b": [reply("got it")],
        },
        channels=AB,
        turn_policy="event_driven",
    )

    outcome = await h.loop.run()

    assert outcome.status == "finished"
    # a goes on until it answers without a tool call, then b, whose first step brings a's mail.
    assert callers(h.store) == ["a", "a", "a", "b"]
    assert users(h.store, "b") == ["Message from a on channel `ab`:\n\nhi"]
    assert [c.turn for _, c in h.store.checkpoints] == [0, 1, 2, 3]
    assert [c.round for _, c in h.store.checkpoints][:3] == [("a", "b"), ("a", "b"), ("a", "b")]


async def test_async_agents_step_concurrently_and_mail_wakes_a_waiting_agent() -> None:
    h = harness(
        (agent("a", tools=("send_message",)), agent("b", tools=("send_message",))),
        {
            "a": [reply("", send("hello", "s1")), reply("done")],
            "b": [reply("idle"), reply("thanks")],
        },
        channels=AB,
        turn_policy="async",
        latency={"a": 0.05, "b": 0.0},
    )

    outcome = await h.loop.run()

    assert outcome.status == "finished"
    # b answered first, waited, and was woken by a's message.
    assert callers(h.store)[0] == "b"
    assert sorted(callers(h.store)) == ["a", "a", "b", "b"]
    assert any("hello" in u for u in users(h.store, "b"))
    assert not h.store.checkpoints  # async runs cannot be forked


async def test_async_max_turns_is_each_agents_own() -> None:
    forever = [reply("", call("shell", '{"cmd": "ls"}', id=f"l{i}")) for i in range(10)]
    h = harness(
        (agent("a"), agent("b", tools=())),
        {"a": forever, "b": [reply("done")]},
        turn_policy="async",
        limits=Limits(max_turns=3),
    )

    outcome = await h.loop.run()

    assert outcome.status == "limit"
    (limit,) = [e for e in h.store.events if isinstance(e.record, LimitRecord)]
    assert limit.agent_id == "a"
    assert callers(h.store).count("a") == 3


async def test_async_delays_in_seconds_hold_a_message() -> None:
    use = ExtensionUse(use=delay.id, config={"channels": ["ab"], "seconds": 0.1})
    h = harness(
        (agent("a", tools=("send_message",)), agent("b", tools=())),
        {"a": [reply("", send("later", "s1")), reply("done")], "b": [reply("idle"), reply("ok")]},
        channels=AB,
        turn_policy="async",
        extensions=[(delay, use)],
    )

    loop = asyncio.get_running_loop()
    began = loop.time()
    outcome = await h.loop.run()

    assert outcome.status == "finished"
    assert any("later" in u for u in users(h.store, "b"))
    assert loop.time() - began >= 0.1
    assert h.store.deliveries[(next(iter(h.store.deliveries))[0], "b")][0] == "delivered"


async def test_wall_clock_ends_the_run_with_a_limit() -> None:
    forever = [reply("", call("shell", '{"cmd": "ls"}', id=f"l{i}")) for i in range(50)]
    h = harness(
        (agent("a"),),
        {"a": forever},
        limits=Limits(wall_clock_s=0.05),
        latency={"a": 0.02},
    )

    outcome = await h.loop.run()

    assert outcome.status == "limit" and outcome.reason == "wall_clock reached (0)"
    last = [e.record for e in h.store.events if isinstance(e.record, LifecycleRecord)][-1]
    assert last.status == "limit"


@extension(id="t.halt", api_version=1)
def halt(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_event")
    async def _(ctx: HookContext[NoState], event: CommittedEvent) -> None:
        if isinstance(event.record, MessageSendRecord):
            ctx.actions.stop("a message was sent")


async def test_async_a_stop_ends_every_agent() -> None:
    forever = [reply("", call("shell", '{"cmd": "ls"}', id=f"l{i}")) for i in range(50)]
    h = harness(
        (agent("a", tools=("send_message",)), agent("b")),
        {"a": [reply("", send("x", "s1")), reply("done")], "b": forever},
        channels=AB,
        turn_policy="async",
        extensions=[halt],
        latency={"b": 0.01},
    )

    outcome = await h.loop.run()

    assert (outcome.status, outcome.reason) == ("stopped", "t.halt: a message was sent")
    assert callers(h.store).count("b") < 10


async def test_async_a_cancel_wakes_agents_waiting_for_mail() -> None:
    h = harness(
        (agent("a", tools=()), agent("b", tools=())),
        {"a": [reply("done")], "b": [reply("done")]},
        turn_policy="async",
        latency={"a": 0.05},
    )
    run = asyncio.create_task(h.loop.run())
    await asyncio.sleep(0.01)
    h.loop.stop("cancelled")

    outcome = await asyncio.wait_for(run, 1)

    assert (outcome.status, outcome.reason) == ("stopped", "cancelled")
