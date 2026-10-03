"""Regressions of the `async` turn policy found in review: capped agents, one limit event, a
cancel and a pause reaching every agent, the wall clock while waiting, and observers settling."""

import asyncio
from typing import Any

import pytest

from swarmeval.runtime.extensions import (
    Delay,
    DeliveryDecision,
    Envelope,
    ExtensionAPI,
    ExtensionError,
    HookContext,
    NoConfig,
    NoState,
    Proceed,
    TurnDecision,
    TurnInfo,
    extension,
)
from swarmeval.runtime.records import (
    CommittedEvent,
    ExtensionEmitRecord,
    LifecycleRecord,
    LimitRecord,
    Transaction,
)
from swarmeval.runtime.specs import Limits
from tests.runtime.fakes import FakePauser, FakeStore, agent, call, harness, reply
from tests.runtime.test_turn_policies import AB, send


class SlowStore(FakeStore):
    """Yields on every commit, as Postgres does, so concurrent agents interleave there."""

    async def commit(self, txn: Transaction) -> list[CommittedEvent]:
        await asyncio.sleep(0.001)
        return await super().commit(txn)


def forever(prefix: str, tokens: int = 10) -> list[Any]:
    return [
        reply("", call("shell", '{"cmd": "ls"}', id=f"{prefix}{i}"), tokens=tokens)
        for i in range(20)
    ]


def limits(h: Any) -> list[CommittedEvent]:
    return [e for e in h.store.events if isinstance(e.record, LimitRecord)]


async def test_an_agent_that_finishes_within_its_turns_is_not_capped() -> None:
    h = harness(
        (agent("a", tools=()),),
        {"a": [reply("done")]},
        turn_policy="async",
        limits=Limits(max_turns=1),
    )

    outcome = await h.loop.run()

    assert outcome.status == "finished" and not limits(h)


async def test_every_agent_capped_ends_the_run_with_a_limit() -> None:
    h = harness(
        (agent("a"), agent("b")),
        {"a": forever("a"), "b": forever("b")},
        turn_policy="async",
        limits=Limits(max_turns=2),
    )

    outcome = await h.loop.run()

    assert outcome.status == "limit"
    assert sorted(e.agent_id or "" for e in limits(h)) == ["a", "b"]


@pytest.mark.parametrize(
    "limit", [Limits(max_tokens=150), Limits(wall_clock_s=0.02)], ids=["tokens", "wall_clock"]
)
async def test_a_run_wide_limit_is_recorded_once(limit: Limits) -> None:
    h = harness(
        (agent("a"), agent("b")),
        {"a": forever("a", 100), "b": forever("b", 100)},
        turn_policy="async",
        limits=limit,
        store=SlowStore(),
        latency={"a": 0.005, "b": 0.005},
    )

    outcome = await h.loop.run()

    assert outcome.status == "limit" and len(limits(h)) == 1


seen: list[str] = []


@extension(id="t.count", api_version=1)
def count(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_turn")
    async def _(ctx: HookContext[NoState], info: TurnInfo) -> TurnDecision:
        assert ctx.agent is not None
        seen.append(ctx.agent.id)
        return Proceed()


async def test_a_cancel_does_not_give_a_waiting_agent_another_turn() -> None:
    seen.clear()
    h = harness(
        (agent("a", tools=()), agent("b", tools=())),
        {"a": [reply("done")], "b": [reply("done")]},
        turn_policy="async",
        latency={"a": 0.05},
        extensions=[count],
    )
    run = asyncio.create_task(h.loop.run())
    await asyncio.sleep(0.01)
    h.loop.stop("cancelled")

    outcome = await asyncio.wait_for(run, 1)

    assert outcome.status == "stopped" and seen.count("b") == 1


@extension(id="t.hold", api_version=1)
def hold(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_turn")
    async def _(ctx: HookContext[NoState], info: TurnInfo) -> TurnDecision:
        if ctx.agent is not None and ctx.agent.id == "a" and info.turn == 1:
            ctx.actions.pause("hold")
        return Proceed()


async def test_a_pause_holds_every_agents_tool_calls() -> None:
    resume = asyncio.Event()
    pauser = FakePauser(resume=resume)
    b_calls = [
        reply(
            "",
            call("shell", '{"cmd": "ls"}', id=f"x{i}"),
            call("shell", '{"cmd": "pwd"}', id=f"y{i}"),
        )
        for i in range(3)
    ]
    h = harness(
        (agent("a", tools=()), agent("b")),
        {"a": [reply("one"), reply("two")], "b": [*b_calls, reply("done")]},
        turn_policy="async",
        latency={"b": 0.02},
        extensions=[hold],
        pauser=pauser,
    )
    run = asyncio.create_task(h.loop.run())
    while not pauser.reasons:
        await asyncio.sleep(0.005)
    held = len(h.sandbox.calls)
    await asyncio.sleep(0.1)

    assert len(h.sandbox.calls) == held
    resume.set()
    assert (await asyncio.wait_for(run, 2)).status == "finished"


@extension(id="t.later", api_version=1)
def later(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_deliver")
    async def _(ctx: HookContext[NoState], envelope: Envelope) -> DeliveryDecision:
        return Delay(seconds=0.5)


async def test_the_wall_clock_runs_out_while_agents_wait_for_held_mail() -> None:
    h = harness(
        (agent("a", tools=("send_message",)), agent("b", tools=())),
        {"a": [reply("", send("x", "s")), reply("done")], "b": [reply("idle"), reply("ok")]},
        channels=AB,
        turn_policy="async",
        extensions=[later],
        limits=Limits(wall_clock_s=0.05),
    )
    clock = asyncio.get_running_loop()
    began = clock.time()

    outcome = await h.loop.run()

    assert outcome.status == "limit" and clock.time() - began < 0.3


@extension(id="t.turns", api_version=1)
def by_turns(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_deliver")
    async def _(ctx: HookContext[NoState], envelope: Envelope) -> DeliveryDecision:
        return Delay(turns=1)


async def test_a_delay_in_turns_fails_an_async_run() -> None:
    h = harness(
        (agent("a", tools=("send_message",)), agent("b", tools=())),
        {"a": [reply("", send("x", "s")), reply("done")], "b": [reply("idle")]},
        channels=AB,
        turn_policy="async",
        extensions=[by_turns],
    )

    with pytest.raises(ExtensionError, match=r"Delay\(turns=\.\.\.\), which the `async`"):
        await h.loop.run()


@extension(id="t.x", api_version=1)
def x(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_event")
    async def _(ctx: HookContext[NoState], event: CommittedEvent) -> None:
        if isinstance(event.record, LifecycleRecord) and event.record.status == "finished":
            ctx.emit("x", 1)


@extension(id="t.y", api_version=1)
def y(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_event")
    async def _(ctx: HookContext[NoState], event: CommittedEvent) -> None:
        if isinstance(event.record, ExtensionEmitRecord) and event.record.name == "x":
            ctx.emit("y", 1)


@pytest.mark.parametrize("policy", ["round_robin", "async"])
async def test_observers_settle_on_each_others_events_at_run_end(policy: Any) -> None:
    h = harness(
        (agent("a", tools=()),),
        {"a": [reply("done")]},
        extensions=[x, y],
        turn_policy=policy,
        store=SlowStore(),
    )

    await h.loop.run()

    names = [e.record.name for e in h.store.events if isinstance(e.record, ExtensionEmitRecord)]
    assert names == ["x", "y"]
