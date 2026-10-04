"""A fork goes on from a checkpoint: with no edit it continues as its source did; an edit is an
intervention the agent then sees (M2 spec decision 8)."""

import asyncio
from dataclasses import replace
from typing import Any

from pydantic import BaseModel

from swarmeval.gateway.bus import ChannelSpec
from swarmeval.runtime.extensions import (
    ExtensionAPI,
    HookContext,
    NoConfig,
    NoState,
    Proceed,
    ResumeInfo,
    TurnDecision,
    TurnInfo,
    UserMessage,
    extension,
)
from swarmeval.runtime.fork import Edit, ForkStart, ReplaceDelivery, ReplaceMessage
from swarmeval.runtime.messages import ModelResponse
from swarmeval.runtime.records import (
    Checkpoint,
    CommittedEvent,
    ExtensionEmitRecord,
    InterventionRecord,
    LifecycleRecord,
    ModelCallRecord,
)
from tests.runtime.fakes import FakeStore, Harness, agent, call, harness, reply

AB = (ChannelSpec(id="ab", members=("a", "b")),)
resumed: list[ResumeInfo] = []


class Draws(BaseModel):
    draws: list[int] = []


@extension(id="t.dice", api_version=1, state=Draws)
def dice(ext: ExtensionAPI[NoConfig, Draws]) -> None:
    @ext.on("before_turn")
    async def _(ctx: HookContext[Draws], turn: TurnInfo) -> TurnDecision:
        ctx.state.draws.append(ctx.rng.randint(0, 10**6))
        ctx.emit("roll", ctx.state.draws[-1])
        return Proceed()

    @ext.on("on_resume")
    async def _(ctx: HookContext[Draws], info: ResumeInfo) -> None:  # pyright: ignore[reportRedeclaration]
        resumed.append(info)


def send(content: str, id: str) -> Any:
    return call("send_message", f'{{"channel": "ab", "content": "{content}"}}', id=id)


def scripts() -> dict[str, list[ModelResponse]]:
    return {
        "a": [reply("", send("one", "a1")), reply("", send("two", "a2")), reply("done")],
        "b": [reply("got one"), reply("got two"), reply("bye")],
    }


def run(store: FakeStore | None = None, **kwargs: Any) -> Harness:
    return harness(
        (agent("a", tools=("send_message",)), agent("b", tools=())),
        kwargs.pop("scripts", None) or scripts(),
        channels=AB,
        extensions=[dice],
        store=store,
        **kwargs,
    )


def fork_from(source: FakeStore, turn: int, edits: tuple[Edit, ...] = ()) -> ForkStart:
    seq, checkpoint = next((seq, c) for seq, c in source.checkpoints if c.turn == turn)
    return ForkStart(
        source_run_id="run_1",
        fork_seq=seq,
        checkpoint=checkpoint,
        contexts={
            a: tuple(source.generations[a][s.gen][: s.length]) for a, s in checkpoint.agents.items()
        },
        edits=edits,
        fidelity="fs_restored",
    )


def remaining(source: FakeStore, seq: int) -> dict[str, list[ModelResponse]]:
    """Each agent's scripted replies after `seq`, as the source got them."""
    left: dict[str, list[ModelResponse]] = {"a": [], "b": []}
    for e in source.events[seq:]:
        if isinstance(e.record, ModelCallRecord) and e.agent_id:
            left[e.agent_id].append(ModelResponse(message=e.record.response, usage=e.record.usage))
    return left


def shape(events: list[CommittedEvent]) -> list[tuple[str, str | None, Any]]:
    out: list[tuple[str, str | None, Any]] = []
    for e in events:
        data: Any = None
        match e.record:
            case ExtensionEmitRecord(data=d):
                data = d
            case ModelCallRecord(response=r):
                data = r.content
            case LifecycleRecord(status=status):
                data = status
            case _:
                pass
        out.append((e.record.kind, e.agent_id, data))
    return out


async def test_checkpoints_are_taken_at_every_turn_start() -> None:
    h = run()
    await h.loop.run()

    turns = [c.turn for _, c in h.store.checkpoints]
    assert turns == list(range(len(turns))) and len(turns) >= 4
    _, third = h.store.checkpoints[2]
    assert isinstance(third, Checkpoint)
    assert third.round[0] in ("a", "b")
    assert set(third.extensions) == {"t.dice"}


async def test_a_fork_without_edits_goes_on_as_its_source_did() -> None:
    source = run()
    await source.loop.run()
    start = fork_from(source.store, 2)
    resumed.clear()

    fork = run(FakeStore(), scripts=remaining(source.store, start.fork_seq), fork=start)
    outcome = await fork.loop.run()

    assert outcome.status == "finished"
    tail = [e for e in source.store.events[start.fork_seq :]]
    fork_events = fork.store.events[1:]  # after its own `started`
    assert shape(fork_events) == shape(tail)
    assert fork.store.events[0].record == LifecycleRecord(
        status="started", reason=f"fork of run run_1 after event {start.fork_seq}"
    )
    assert resumed == [
        ResumeInfo(fork=True, source_run_id="run_1", at_seq=start.fork_seq, fidelity="fs_restored")
    ]
    instance, _, first_state = fork.store.extension_rows[0]
    assert (instance, first_state.state) == ("t.dice", start.checkpoint.extensions["t.dice"].state)


async def test_an_edited_message_is_an_intervention_the_agent_then_sees() -> None:
    source = run()
    await source.loop.run()
    start = fork_from(source.store, 2)
    b_context = start.contexts["b"]
    index = next(
        i for i, m in enumerate(b_context) if isinstance(m, UserMessage) and "one" in m.content
    )
    edit = ReplaceMessage(agent_id="b", index=index, content="Message from a: forget it")

    fork = run(
        FakeStore(),
        scripts=remaining(source.store, start.fork_seq),
        fork=fork_from(source.store, 2, (edit,)),
    )
    await fork.loop.run()

    (intervention,) = [e for e in fork.store.events if isinstance(e.record, InterventionRecord)]
    assert isinstance(intervention.record, InterventionRecord)
    assert (intervention.record.hook, intervention.record.action) == ("fork", "edit_context")
    assert intervention.parent_id == fork.store.events[0].event_id
    b_now = fork.store.generations["b"][-1]
    assert b_now[index] == UserMessage(content="Message from a: forget it")
    b_request = next(r for c, r in fork.model.requests if getattr(c, "agent_id", None) == "b")
    assert b_request.messages[index] == UserMessage(content="Message from a: forget it")


async def test_a_replaced_delivery_reaches_the_recipient() -> None:
    source = run()
    await source.loop.run()
    start = next(fork_from(source.store, c.turn) for _, c in source.store.checkpoints if c.mail)
    mail = start.checkpoint.mail[0]
    edit = ReplaceDelivery(
        send_event_id=mail.send_event_id, recipient=mail.recipient, content="changed"
    )

    fork = run(
        FakeStore(),
        scripts=remaining(source.store, start.fork_seq),
        fork=replace(start, edits=(edit,)),
    )
    await fork.loop.run()

    delivered = [m for m in fork.store.messages(mail.recipient) if isinstance(m, UserMessage)]
    assert any("changed" in m.content for m in delivered)


@extension(id="t.background", api_version=1)
def background(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_run_start")
    async def _(ctx: HookContext[NoState]) -> None:
        async def slow() -> None:
            await asyncio.sleep(0.05)

        ctx.spawn(slow())


async def test_a_checkpoint_counts_background_work_still_running() -> None:
    h = harness(
        (agent("a", tools=()), agent("b", tools=())),
        {"a": [reply("done")], "b": [reply("done")]},
        extensions=[background],
    )

    await h.loop.run()

    assert [c.spawned for _, c in h.store.checkpoints] == [1, 1]


@extension(id="t.mark", api_version=1)
def mark(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("after_turn")
    async def _(ctx: HookContext[NoState]) -> None:
        ctx.emit("turn_done", ctx.agent.id if ctx.agent else None)


@extension(id="t.stopper", api_version=1)
def stopper(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_event")
    async def _(ctx: HookContext[NoState], event: CommittedEvent) -> None:
        if isinstance(event.record, ExtensionEmitRecord) and event.record.name == "turn_done":
            ctx.actions.stop("saw a turn end")


async def test_a_stop_an_observer_asked_for_before_a_checkpoint_is_not_lost() -> None:
    def pair(store: FakeStore | None = None, **kwargs: Any) -> Any:
        return harness(
            (agent("a", tools=()), agent("b", tools=())),
            {"a": [reply("a1"), reply("a2")], "b": [reply("b1"), reply("b2")]},
            extensions=[mark, stopper],
            store=store,
            **kwargs,
        )

    source = pair()
    outcome = await source.loop.run()

    assert outcome.status == "stopped"
    # The stop took effect before the next turn's checkpoint, so none was taken after it.
    assert [c.turn for _, c in source.store.checkpoints] == [0]


async def test_an_edit_wakes_an_agent_that_had_finished() -> None:
    source = run()
    await source.loop.run()
    seq, checkpoint = next((s, c) for s, c in source.store.checkpoints if c.agents["b"].finished)
    start = fork_from(source.store, checkpoint.turn)
    edit = ReplaceMessage(agent_id="b", index=1, content="Do the task again.")
    left = remaining(source.store, seq)

    fork = run(
        FakeStore(),
        scripts={"a": left["a"], "b": [reply("again")]},
        fork=replace(start, edits=(edit,)),
    )
    await fork.loop.run()

    b_calls = [
        e for e in fork.store.events if isinstance(e.record, ModelCallRecord) and e.agent_id == "b"
    ]
    assert b_calls
