"""The online and offline adapters give the same view of the same event."""

import json
from typing import Any

from swarmeval.detect.view import EventView, view_of, view_of_row
from swarmeval.events.seal import ChainHead, seal
from swarmeval.gateway.bus import ChannelSpec
from swarmeval.runtime.messages import AssistantMessage, ModelResponse, Usage
from swarmeval.runtime.records import (
    CommittedEvent,
    EventDraft,
    ExecResult,
    FinalDiffRecord,
    FsChange,
    ProcessInfo,
)
from tests.runtime.fakes import FakeSandbox, agent, call, harness, reply
from tests.runtime.test_deliver import upper

CHANGE = FsChange.model_validate(
    {
        "path": "/workspace/tests/t.py",
        "op": "modify",
        "uid": 0,
        "before_sha256": "aa",
        "after_sha256": "bb",
        "protected": True,
    }
)
RESULT = ExecResult(
    exit_code=0,
    stdout="ok",
    stderr="",
    fs_changes=(CHANGE,),
    processes=(ProcessInfo(pid=7, ppid=1, user="root", cmdline="sleep 9"),),
)


def rows(events: list[CommittedEvent], sandboxes: dict[str, str | None]) -> list[dict[str, Any]]:
    drafts = [
        EventDraft(
            record=e.record,
            agent_id=e.agent_id,
            extension=e.extension,
            parent_id=e.parent_id,
            event_id=e.event_id,
        )
        for e in events
    ]
    _, sealed, _ = seal(
        drafts, ChainHead.start("run_1"), run_id="run_1", workspace="ws", sandboxes=sandboxes
    )
    return [{**r, "payload": json.dumps(r["payload"])} for r in sealed]


async def run_events() -> list[CommittedEvent]:
    thinking = ModelResponse(
        message=AssistantMessage(
            content="",
            reasoning="first the tests",
            tool_calls=(
                call("shell", '{"cmd": "edit"}', id="c1"),
                call("send_message", '{"channel": "ab", "content": "psst"}', id="c2"),
            ),
        ),
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    h = harness(
        (agent("a", tools=("shell", "send_message")), agent("b", tools=())),
        {"a": [thinking, reply("done")], "b": [reply("ok")]},
        channels=(ChannelSpec(id="ab", members=("a", "b")),),
        sandbox=FakeSandbox(handler=lambda _: RESULT),
        extensions=[upper],
    )
    await h.loop.run()
    return h.store.events


async def test_both_adapters_agree_on_every_event() -> None:
    events = await run_events()
    events.append(
        CommittedEvent(
            event_id="final",
            seq=len(events) + 1,
            agent_id=None,
            extension=None,
            parent_id=None,
            record=FinalDiffRecord(sandbox_id="box_a", changes=(CHANGE,)),
        )
    )
    sandboxes: dict[str, str | None] = {"a": "box_a", "b": "box_b"}

    online = [view_of(e, sandboxes) for e in events]
    offline = [view_of_row(r) for r in rows(events, sandboxes)]

    assert offline == online
    by_kind: dict[str, list[EventView]] = {}
    for view in online:
        by_kind.setdefault(view.kind, []).append(view)
    model = by_kind["model"][0]
    assert [(t.role, t.field) for t in model.texts] == [
        ("model_output", "content"),
        ("model_output", "reasoning"),
        ("model_output", "tool_calls.c1.arguments"),
        ("model_output", "tool_calls.c2.arguments"),
    ]
    tool = by_kind["tool"][0]
    assert (tool.sandbox_id, tool.tool, tool.changes[0].change.path) == (
        "box_a",
        "shell",
        CHANGE.path,
    )
    assert tool.processes[0].cmdline == "sleep 9"
    (send,) = by_kind["msg.send"]
    assert (send.channel, send.sender, send.recipients) == ("ab", "a", ("b",))
    rewrite = next(v for v in by_kind["intervention"] if v.texts)
    assert (rewrite.texts[0].role, rewrite.texts[0].text, rewrite.recipients) == (
        "rewritten_message",
        "PSST",
        ("b",),
    )
    (deliver,) = by_kind["msg.deliver"]
    assert [(t.role, t.text) for t in deliver.texts] == [("delivered", "PSST")]
    assert by_kind["final_diff"][0].changes[0].sandbox_id == "box_a"
