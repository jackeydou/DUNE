"""`swarmeval.monitor`: a hit is an alert on the event that caused it, and the action follows
with the alert as its cause (M2 spec decisions 1 and 7)."""

import asyncio
import json
from typing import Any

import pytest

from swarmeval.core.interventions import expand
from swarmeval.core.models import CaseFile
from swarmeval.gateway.bus import ChannelSpec
from swarmeval.runtime.extensions import (
    ExtensionLoadError,
    ExtensionUse,
    UserMessage,
    load_extensions,
)
from swarmeval.runtime.extensions.registry import resolve_entry_point
from swarmeval.runtime.records import (
    AlertRecord,
    CommittedEvent,
    InterventionRecord,
    LifecycleRecord,
    MessageSendRecord,
)
from tests.runtime.fakes import FakePauser, Harness, agent, call, harness, reply
from tests.runtime.test_causal import assert_rooted

AB = (ChannelSpec(id="ab", members=("a", "b")),)
MONITOR = resolve_entry_point("swarmeval.monitor")


def send(content: str, id: str) -> Any:
    return call("send_message", json.dumps({"channel": "ab", "content": content}), id=id)


def run(config: dict[str, Any], *, a_replies: int = 1, pauser: FakePauser | None = None) -> Harness:
    use = ExtensionUse(use="swarmeval.monitor", config=config)
    return harness(
        (agent("a", tools=("send_message",)), agent("b", tools=())),
        {
            "a": [reply("", send("hold​", "s1"), send("again​", "s2"))] + [reply("done")] * a_replies,
            "b": [reply("ok"), reply("ok")],
        },
        channels=AB,
        extensions=[(MONITOR, use)],
        pauser=pauser,
    )


def one(events: list[CommittedEvent], kind: type) -> list[CommittedEvent]:
    return [e for e in events if isinstance(e.record, kind)]


ZERO_WIDTH: dict[str, Any] = {"detectors": [{"detector": "zero_width"}]}


async def test_each_hit_is_an_alert_on_the_event_that_caused_it() -> None:
    h = run(ZERO_WIDTH)

    outcome = await h.loop.run()

    assert outcome.status == "finished"
    events = h.store.events
    sends = one(events, MessageSendRecord)
    alerts = one(events, AlertRecord)
    assert [a.parent_id for a in alerts] == [s.event_id for s in sends]
    first = alerts[0].record
    assert isinstance(first, AlertRecord)
    assert first.severity == "high" and first.event_ids == (sends[0].event_id,)
    assert first.message.startswith("zero_width: 1 invisible character(s) in message content")
    assert all(a.extension == "swarmeval.monitor" for a in alerts)
    assert_rooted(events)


async def test_pause_holds_the_run_until_resumed_and_records_why() -> None:
    resume = asyncio.Event()
    pauser = FakePauser(resume=resume)
    h = run({**ZERO_WIDTH, "on_hit": "pause"}, pauser=pauser)

    task = asyncio.create_task(h.loop.run())
    while not pauser.reasons:
        await asyncio.sleep(0)
    paused_at = len(h.store.events)
    await asyncio.sleep(0.01)
    assert len(h.store.events) == paused_at  # nothing happens while paused
    resume.set()
    outcome = await task

    assert outcome.status == "finished"
    events = h.store.events
    by_id = {e.event_id: e for e in events}
    (pause,) = [
        e
        for e in one(events, InterventionRecord)
        if isinstance(e.record, InterventionRecord) and e.record.action == "pause"
    ]
    lifecycles = one(events, LifecycleRecord)
    statuses = [e.record.status for e in lifecycles if isinstance(e.record, LifecycleRecord)]
    assert statuses == ["started", "paused", "resumed", "finished"]
    paused, resumed = lifecycles[1], lifecycles[2]
    assert paused.parent_id == pause.event_id and resumed.parent_id == paused.event_id
    alert = by_id[pause.parent_id or ""]
    assert isinstance(alert.record, AlertRecord)
    assert pauser.reasons == [f"swarmeval.monitor: zero_width hit on event {alert.parent_id}"]
    assert_rooted(events)


async def test_stop_ends_the_run_from_the_alert() -> None:
    h = run({**ZERO_WIDTH, "on_hit": "stop"})

    outcome = await h.loop.run()

    assert outcome.status == "stopped"
    last = one(h.store.events, LifecycleRecord)[-1]
    stop = next(e for e in h.store.events if e.event_id == last.parent_id)
    assert isinstance(stop.record, InterventionRecord) and stop.record.action == "stop"
    alert = next(e for e in h.store.events if e.event_id == stop.parent_id)
    assert isinstance(alert.record, AlertRecord)


async def test_inject_reaches_the_sender_once_by_default() -> None:
    h = run({**ZERO_WIDTH, "on_hit": "inject", "inject": {"content": "We saw that."}})

    await h.loop.run()

    injected = [
        m
        for m in h.store.messages("a")
        if isinstance(m, UserMessage) and m.content == "We saw that."
    ]
    assert len(injected) == 1  # two hits, `max_actions: 1`
    assert len(one(h.store.events, AlertRecord)) == 2


async def test_detector_state_is_committed_with_the_extension_state() -> None:
    h = run({"detectors": [{"detector": "zero_width"}, {"detector": "cross_sandbox"}]})

    await h.loop.run()

    instance, _, snapshot = h.store.extension_rows[-1]
    assert instance == "swarmeval.monitor"
    assert isinstance(snapshot.state, dict)
    assert snapshot.state["detectors"] == [None, {}]
    assert snapshot.state["hits"] == 2


def test_a_bad_monitor_config_is_refused_at_load() -> None:
    with pytest.raises(ExtensionLoadError, match="`inject` is required with `on_hit: inject`"):
        load_extensions(
            [ExtensionUse(use="swarmeval.monitor", config={**ZERO_WIDTH, "on_hit": "inject"})]
        )


def test_the_case_loader_checks_the_inject_target() -> None:
    case = CaseFile.model_validate(
        {
            "schema_version": 3,
            "id": "c",
            "workspace": "w",
            "swarm": {"agents": [{"id": "a", "model": "m", "prompt": "p.md", "task": "t.md"}]},
            "extensions": [
                {
                    "use": "swarmeval.monitor",
                    "config": {
                        **ZERO_WIDTH,
                        "on_hit": "inject",
                        "inject": {"content": "x", "agent": "zed"},
                    },
                }
            ],
        }
    )
    with pytest.raises(ValueError, match=r"`inject\.agent` is `zed`, which is not an agent"):
        expand(case)
