"""The built-in channel interventions on in-memory runs: drop, delay, paraphrase, inject."""

from typing import Any

import pytest
from pydantic import ValidationError

from swarmeval.gateway.bus import ChannelSpec
from swarmeval.gateway.bus.interventions import (
    DEFAULT_PARAPHRASE_PROMPT,
    DelayConfig,
    delay,
    drop,
    inject,
    paraphrase,
)
from swarmeval.runtime.extensions import Extension, ExtensionUse, SystemMessage, UserMessage
from swarmeval.runtime.ports import ExtensionCaller
from swarmeval.runtime.records import InterventionRecord, MessageDeliverRecord, MessageSendRecord
from tests.runtime.fakes import Harness, agent, call, harness, reply
from tests.runtime.test_causal import assert_rooted
from tests.worker.test_transcript import check

CHANNELS = (
    ChannelSpec(id="dm", members=("a", "b")),
    ChannelSpec(id="side", members=("a", "b")),
)


def send(channel: str, content: str, id: str) -> Any:
    return call("send_message", f'{{"channel": "{channel}", "content": "{content}"}}', id=id)


def use(ext: Extension[Any, Any], config: dict[str, Any], alias: str | None = None) -> Any:
    raw: dict[str, Any] = {"use": ext.id, "config": config}
    if alias:
        raw["as"] = alias
    return (ext, ExtensionUse.model_validate(raw))


def chatter(*extensions: Any, messages: int = 6, seed: int = 7, b_replies: int = 1) -> Harness:
    """a sends `messages` messages on `dm` in one turn, then stops; b reads and stops."""
    sends = [send("dm", f"m{i}", f"c{i}") for i in range(messages)]
    return harness(
        (agent("a", tools=("send_message",)), agent("b", tools=())),
        {"a": [reply("", *sends), reply("done")], "b": [reply("ok")] * b_replies},
        channels=CHANNELS,
        extensions=extensions,
        seed=seed,
    )


def delivered(h: Harness) -> list[str]:
    return [
        e.record.content
        for e in h.store.records("msg.deliver")
        if isinstance(e.record, MessageDeliverRecord)
    ]


async def test_drop_with_p_one_drops_every_message_on_its_channels_only() -> None:
    h = harness(
        (agent("a", tools=("send_message",)), agent("b", tools=())),
        {
            "a": [reply("", send("dm", "lost", "c1"), send("side", "kept", "c2")), reply("ok")],
            "b": [reply("ok")],
        },
        channels=CHANNELS,
        extensions=[use(drop, {"channels": ["dm"], "p": 1.0})],
    )

    await h.loop.run()

    assert delivered(h) == ["kept"]
    assert sorted(status for status, _ in h.store.deliveries.values()) == [
        "delivered",
        "dropped",
    ]
    assert check(h).consistent


async def test_drop_is_fixed_by_the_seed() -> None:
    def outcome(seed: int) -> Any:
        return chatter(use(drop, {"channels": ["dm"], "p": 0.5}), messages=12, seed=seed)

    runs = [outcome(seed) for seed in (3, 3, 4)]
    for h in runs:
        await h.loop.run()

    kept = [delivered(h) for h in runs]
    assert kept[0] == kept[1] != kept[2]
    assert 0 < len(kept[0]) < 12
    # Each draw advanced the instance's stream count, committed with its state.
    counts = [v.rng_uses for i, _, v in runs[0].store.extension_rows if i == "swarmeval.bus.drop"]
    assert counts == list(range(1, 13))


async def test_delay_holds_for_a_fixed_count_or_a_seeded_draw() -> None:
    fixed = chatter(use(delay, {"channels": ["dm"], "turns": 2}), messages=1, b_replies=2)
    await fixed.loop.run()
    assert list(fixed.store.deliveries.values()) == [("delayed", 3)]

    def ranged(seed: int) -> Harness:
        return chatter(
            use(delay, {"channels": ["dm"], "turns": [1, 4]}), messages=8, seed=seed, b_replies=5
        )

    runs = [ranged(seed) for seed in (5, 5, 6)]
    for h in runs:
        await h.loop.run()
    dues = [[due for _, due in h.store.deliveries.values()] for h in runs]
    assert dues[0] == dues[1] != dues[2]
    # b has taken no turn when a sends, so a hold of n turns makes it due at b's turn n + 1.
    assert {due for due in dues[0] if due is not None} <= {2, 3, 4, 5}


def test_a_delay_range_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match=r"Write \[1, 3\]"):
        DelayConfig.model_validate({"channels": ["dm"], "turns": [3, 1]})


async def test_paraphrase_calls_the_model_under_its_own_key_and_delivers_the_rewrite() -> None:
    h = harness(
        (agent("a", tools=("send_message",)), agent("b", tools=())),
        {
            "a": [reply("", send("dm", "Price at 10. Agreed?", "c1")), reply("done")],
            "b": [reply("ok")],
            "dm_para": [reply("Is ten the price we agree on?")],
        },
        channels=CHANNELS,
        extensions=[use(paraphrase, {"channels": ["dm"], "model": "small"}, alias="dm_para")],
    )

    await h.loop.run()

    caller, request = h.model.requests[1]
    assert caller == ExtensionCaller("dm_para")
    assert request.model == "small"
    assert request.messages == (
        SystemMessage(content=DEFAULT_PARAPHRASE_PROMPT),
        UserMessage(content="Price at 10. Agreed?"),
    )
    (send_event,) = h.store.records("msg.send")
    (rewrite_call,) = [e for e in h.store.records("model") if e.extension == "dm_para"]
    assert (rewrite_call.agent_id, rewrite_call.parent_id) == (None, send_event.event_id)
    assert delivered(h) == ["Is ten the price we agree on?"]
    (rewrite,) = h.store.records("intervention")
    assert isinstance(rewrite.record, InterventionRecord)
    assert rewrite.record.after == {
        "recipient": "b",
        "kind": "deliver",
        "content": "Is ten the price we agree on?",
    }
    result = check(h)
    assert result.consistent, result.mismatches
    assert result.interventions == (rewrite.event_id,) and result.deliveries == 1
    assert_rooted(h.store.events)


async def test_inject_posts_on_the_channel_at_its_turn_as_the_named_sender() -> None:
    config = {"channel": "dm", "at_turn": 2, "sender": "a", "content": "Let's both charge 12."}
    h = harness(
        (agent("a", tools=("shell",)), agent("b", tools=())),
        {
            "a": [reply("", call("shell", '{"cmd": "ls"}')), reply("done")],
            "b": [reply("hm"), reply("ok")],
        },
        channels=CHANNELS,
        extensions=[use(inject, config, alias="forge")],
    )

    await h.loop.run()

    (post,) = h.store.records("intervention")
    assert isinstance(post.record, InterventionRecord)
    assert (post.record.hook, post.record.action, post.agent_id) == ("before_turn", "post", "b")
    (sent,) = h.store.records("msg.send")
    assert isinstance(sent.record, MessageSendRecord)
    assert (sent.agent_id, sent.extension, sent.parent_id) == (None, "forge", post.event_id)
    assert (sent.record.sender, sent.record.recipients, sent.record.call_id) == ("a", ("b",), None)
    # Posted at b's turn (run-wide turn 2), after b's mail was taken: b reads it next turn.
    assert delivered(h) == ["Let's both charge 12."]
    assert h.store.messages("b")[-2] == UserMessage(
        content="Message from a on channel `dm`:\n\nLet's both charge 12."
    )
    result = check(h)
    assert result.consistent, result.mismatches
    assert post.event_id in result.interventions
    assert_rooted(h.store.events)


async def test_the_transcript_check_explains_drops_and_delays() -> None:
    h = chatter(
        use(drop, {"channels": ["dm"], "p": 0.5}),
        use(delay, {"channels": ["dm"], "turns": 1}),
        messages=6,
        b_replies=2,
    )

    await h.loop.run()

    result = check(h)
    assert result.consistent, result.mismatches
    assert result.deliveries == len(h.store.records("msg.deliver")) > 0
    assert len(result.interventions) == len(h.store.records("intervention"))
