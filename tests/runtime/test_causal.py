"""Every event names the event that directly caused it (docs/event-log.md#causal-parents), and
every chain ends at the run's first event."""

from collections.abc import Sequence

from swarmeval.gateway.bus import ChannelSpec
from swarmeval.runtime.extensions import (
    CommittedEvent,
    ExtensionAPI,
    HookContext,
    NoConfig,
    NoState,
    RequestOptions,
    ToolResult,
    UserMessage,
    extension,
)
from swarmeval.runtime.loop import Limits
from swarmeval.runtime.messages import ModelRequest
from swarmeval.runtime.records import (
    AlertRecord,
    InterventionRecord,
    LifecycleRecord,
    ToolCallRecord,
)
from tests.runtime.fakes import agent, call, harness, reply

TEAM = (ChannelSpec(id="team", members=("dev", "qa")),)


def assert_rooted(events: Sequence[CommittedEvent]) -> None:
    """Only the first event has no parent, and following parents from any event reaches it
    through events committed earlier."""
    by_id = {e.event_id: e for e in events}
    root = events[0]
    assert root.parent_id is None
    for event in events[1:]:
        current = event
        while current.parent_id is not None:
            parent = by_id[current.parent_id]
            assert parent.seq < current.seq, (current, parent)
            current = parent
        assert current is root, f"{event.record.kind} #{event.seq} ends at #{current.seq}"


def parent(events: Sequence[CommittedEvent], event: CommittedEvent) -> CommittedEvent:
    return next(e for e in events if e.event_id == event.parent_id)


@extension(id="t.tag", api_version=1)
def tag(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("after_tool_result")
    async def _(ctx: HookContext[NoState], result: ToolResult) -> ToolResult:
        return result.model_copy(update={"content": result.content + " [checked]"})


async def test_each_event_names_what_caused_it() -> None:
    send = call("send_message", '{"channel": "team", "content": "ready?"}')
    h = harness(
        (agent("dev", tools=("shell", "send_message")), agent("qa", tools=())),
        {
            "dev": [reply("", call("shell", '{"cmd": "ls"}')), reply("", send), reply("done")],
            "qa": [reply("waiting"), reply("yes")],
        },
        channels=TEAM,
        extensions=[tag],
    )

    await h.loop.run()

    events = h.store.events
    assert_rooted(events)
    started = events[0]
    assert started.record == LifecycleRecord(status="started")
    dev_calls = [e for e in h.store.records("model") if e.agent_id == "dev"]
    qa_calls = [e for e in h.store.records("model") if e.agent_id == "qa"]
    # A first call reads the generation `started` admitted.
    assert dev_calls[0].parent_id == started.event_id
    assert qa_calls[0].parent_id == started.event_id
    # A tool result rewritten by an extension: the agent's next call read the rewrite.
    tool = h.store.records("tool")[0]
    assert tool.parent_id == dev_calls[0].event_id
    rewrite = parent(events, dev_calls[1])
    assert isinstance(rewrite.record, InterventionRecord)
    assert rewrite.parent_id == tool.event_id
    # The send hangs off the call that sent it, the delivery off the send, and qa's next call
    # off the delivery.
    (msg_send,) = h.store.records("msg.send")
    assert msg_send.parent_id == dev_calls[1].event_id
    (deliver,) = h.store.records("msg.deliver")
    assert deliver.parent_id == msg_send.event_id
    assert qa_calls[1].parent_id == deliver.event_id
    send_result = parent(events, dev_calls[2])
    assert parent(events, send_result).record.kind == "tool"
    finished = events[-1]
    assert finished.record == LifecycleRecord(status="finished")
    assert finished.parent_id == started.event_id


@extension(id="t.monitor", api_version=1)
def monitor(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_event")
    async def _(ctx: HookContext[NoState], event: CommittedEvent) -> None:
        if isinstance(event.record, ToolCallRecord):
            ctx.emit("saw_tool", {"call": event.record.call.id})
            alert = ctx.actions.alert("a tool ran", severity="high", event_ids=[event.event_id])
            ctx.actions.inject("a", "Stop using tools.", cause=alert)
            ctx.actions.stop("a tool ran", cause=alert)


@extension(id="t.judge", api_version=1)
def judge(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("after_turn")
    async def _(ctx: HookContext[NoState]) -> None:
        request = ModelRequest(
            model="judge-model",
            messages=(UserMessage(content="Was it fine?"),),
            options=RequestOptions(tools=()),
        )
        await ctx.model.generate(request)


async def test_actions_descend_from_the_alert_that_caused_them() -> None:
    h = harness(
        (agent("a"),),
        {"a": [reply("", call("shell", '{"cmd": "ls"}')), reply("done")], "t.judge": [reply("ok")]},
        extensions=[judge, monitor],
    )

    outcome = await h.loop.run()

    assert outcome.status == "stopped"
    events = h.store.events
    assert_rooted(events)
    tool = h.store.records("tool")[0]
    (emitted,) = h.store.records("extension")
    assert emitted.parent_id == tool.event_id
    (alert,) = h.store.records("alert")
    assert isinstance(alert.record, AlertRecord)
    assert alert.parent_id == tool.event_id
    actions = [e for e in h.store.records("intervention") if e.extension == "t.monitor"]
    assert [e.parent_id for e in actions] == [alert.event_id] * 2
    stop = next(e for e in actions if e.record.model_dump()["action"] == "stop")
    end = events[-1]
    assert end.record == LifecycleRecord(status="stopped", reason="t.monitor: a tool ran")
    assert end.parent_id == stop.event_id
    # The judge's own call hangs off what the agent last read when its turn ended.
    (judge_call,) = [e for e in h.store.records("model") if e.extension == "t.judge"]
    assert judge_call.parent_id == tool.event_id


async def test_a_limit_ends_the_run_through_its_own_event() -> None:
    h = harness(
        (agent("a"),), {"a": [reply("", call("shell", '{"cmd": "ls"}'))] * 3}, limits=Limits(2)
    )

    await h.loop.run()

    events = h.store.events
    assert_rooted(events)
    (limit,) = h.store.records("limit")
    assert limit.parent_id == events[0].event_id
    assert events[-1].parent_id == limit.event_id
