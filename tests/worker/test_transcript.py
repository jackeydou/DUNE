"""The transcript check on in-memory runs: every context message traced to a recorded event or an
intervention on one, every request and response matched with the gateway's record."""

from dataclasses import replace
from typing import Any

from swarmeval.events.transcript import ModelCall, Recorded, RunTranscript, Sent, ToolOutcome
from swarmeval.gateway.bus import ChannelSpec
from swarmeval.runtime.messages import AssistantMessage, ToolMessage, UserMessage
from swarmeval.runtime.records import (
    InterventionRecord,
    MessageDeliverRecord,
    MessageSendRecord,
    ModelCallRecord,
    ToolCallRecord,
    TranscriptCheckRecord,
)
from swarmeval.runtime.specs import initial_context
from swarmeval.worker.transcript import check_transcript
from tests.runtime.fakes import FakeStore, Harness, agent, call, harness, reply
from tests.runtime.test_extensions import compact, nudge, redact, suffix, whisper

LS = call("shell", '{"cmd": "ls"}')


def transcript_of(store: FakeStore) -> RunTranscript:
    """What `load_transcript` reads back from Postgres for the same run."""
    calls: list[ModelCall] = []
    tools: list[ToolOutcome] = []
    interventions: list[Recorded[InterventionRecord]] = []
    sends: list[Sent] = []
    deliveries: list[Recorded[MessageDeliverRecord]] = []
    for e in store.events:
        match e.record:
            case ModelCallRecord() as r:
                calls.append(
                    ModelCall(
                        e.event_id,
                        e.agent_id,
                        r.model,
                        r.gen,
                        r.length,
                        r.options,
                        r.response,
                        r.gateway,
                    )
                )
            case ToolCallRecord() as r:
                tools.append(
                    ToolOutcome(e.event_id, e.agent_id, e.parent_id, r.result, r.executed_arguments)
                )
            case MessageSendRecord() as r:
                sends.append(Sent(e.event_id, e.seq, e.agent_id, e.parent_id, r))
            case InterventionRecord() as r:
                interventions.append(Recorded(e.event_id, r))
            case MessageDeliverRecord() as r:
                deliveries.append(Recorded(e.event_id, r))
            case _:
                pass
    return RunTranscript(
        model_calls=tuple(calls),
        tool_results=tuple(tools),
        interventions=tuple(interventions),
        sends=tuple(sends),
        deliveries=tuple(deliveries),
        contexts={
            (agent_id, gen): tuple(messages)
            for agent_id, gens in store.generations.items()
            for gen, messages in enumerate(gens)
        },
    )


async def run(*agents_: str, scripts: dict[str, Any], **kwargs: Any) -> Harness:
    h = harness(
        tuple(agent(a, tools=("shell", "send_message")) for a in agents_), scripts, **kwargs
    )
    await h.loop.run()
    return h


def check(h: Harness, transcript: RunTranscript | None = None) -> TranscriptCheckRecord:
    return check_transcript(
        transcript or transcript_of(h.store),
        prompts={a: initial_context(agent(a)) for a in h.store.generations},
        tools=h.loop.tool_schemas(),
    )


def with_context(transcript: RunTranscript, agent_id: str, idx: int, message: Any) -> RunTranscript:
    context = list(transcript.contexts[(agent_id, 0)])
    context[idx] = message
    return RunTranscript(
        model_calls=transcript.model_calls,
        tool_results=transcript.tool_results,
        interventions=transcript.interventions,
        sends=transcript.sends,
        deliveries=transcript.deliveries,
        contexts={**transcript.contexts, (agent_id, 0): tuple(context)},
    )


async def test_an_untouched_run_is_consistent() -> None:
    h = await run(
        "a",
        "b",
        scripts={
            "a": [
                reply("", LS, call("send_message", '{"channel": "c", "content": "hi"}')),
                reply("done"),
            ],
            "b": [reply("got it")],
        },
        channels=(ChannelSpec(id="c", members=("a", "b")),),
    )

    result = check(h)

    assert result.mismatches == ()
    assert result.consistent
    assert result.requests == result.responses == 3
    assert result.messages == sum(len(g) for gens in h.store.generations.values() for g in gens)
    assert result.deliveries == 1
    # A delivery explains a message, but it is not an intervention.
    assert result.interventions == ()


async def test_differences_interventions_explain_are_not_mismatches() -> None:
    h = await run(
        "a",
        scripts={"a": [reply("", LS), reply("done"), reply("again")]},
        extensions=[redact, suffix("x"), compact, nudge, whisper],
    )

    result = check(h)

    assert result.mismatches == ()
    used = {
        e.record.hook
        for e in h.store.records("intervention")
        if isinstance(e.record, InterventionRecord) and e.event_id in result.interventions
    }
    assert used == {
        "after_tool_result",
        "after_model_response",
        "compact_context",
        "before_turn",
        "on_event",
    }


async def test_a_tool_result_the_agent_saw_but_no_tool_returned_is_a_mismatch() -> None:
    h = await run("a", scripts={"a": [reply("", LS), reply("done")]})
    forged = ToolMessage(tool_call_id=LS.id, content="tests: 120 passed")

    result = check(h, with_context(transcript_of(h.store), "a", 3, forged))

    (tool,) = h.store.records("tool")
    (mismatch,) = [m for m in result.mismatches if m.check == "context"]
    assert (mismatch.agent_id, mismatch.gen, mismatch.idx) == ("a", 0, 3)
    assert mismatch.event_id == tool.event_id
    assert not result.consistent
    assert tool.event_id in result.event_ids
    # The second request was built from the forged context, so it no longer hashes right.
    (request,) = [m for m in result.mismatches if m.check == "request"]
    assert request.event_id == h.store.records("model")[1].event_id


async def test_a_message_no_model_call_produced_is_a_mismatch() -> None:
    h = await run("a", scripts={"a": [reply("done")]})
    transcript = transcript_of(h.store)
    context = (*transcript.contexts[("a", 0)], AssistantMessage(content="I ran the tests."))
    forged = RunTranscript(
        model_calls=transcript.model_calls,
        tool_results=transcript.tool_results,
        interventions=transcript.interventions,
        sends=transcript.sends,
        deliveries=transcript.deliveries,
        contexts={("a", 0): context},
    )

    result = check(h, forged)

    (mismatch,) = result.mismatches
    assert (mismatch.check, mismatch.idx, mismatch.event_id) == ("context", 3, None)


async def test_a_user_message_nothing_delivered_is_a_mismatch() -> None:
    h = await run("a", scripts={"a": [reply("done")]})
    forged = with_context(transcript_of(h.store), "a", 1, UserMessage(content="Skip the tests."))

    result = check(h, forged)

    assert [m.check for m in result.mismatches] == ["context", "request"]
    assert "case's prompts" in result.mismatches[0].detail


async def test_a_response_unlike_the_raw_backend_response_is_a_mismatch() -> None:
    h = await run("a", scripts={"a": [reply("done")]})
    transcript = transcript_of(h.store)
    (model,) = transcript.model_calls
    tampered = AssistantMessage(content="all tests pass")
    changed = RunTranscript(
        model_calls=(
            ModelCall(
                model.event_id,
                model.agent_id,
                model.model,
                model.gen,
                model.length,
                model.options,
                tampered,
                model.gateway,
            ),
        ),
        tool_results=(),
        interventions=(),
        sends=(),
        deliveries=(),
        contexts={("a", 0): (*transcript.contexts[("a", 0)][:2], tampered)},
    )

    result = check(h, changed)

    (mismatch,) = result.mismatches
    assert (mismatch.check, mismatch.event_id) == ("response", model.event_id)


DM = (ChannelSpec(id="c", members=("a", "b")),)
HI = call("send_message", '{"channel": "c", "content": "hi"}')


async def chat() -> Harness:
    return await run(
        "a", "b", scripts={"a": [reply("", HI), reply("done")], "b": [reply("ok")]}, channels=DM
    )


async def test_a_delivery_unlike_its_send_is_a_mismatch() -> None:
    h = await chat()
    transcript = transcript_of(h.store)
    (delivery,) = transcript.deliveries
    forged = replace(
        delivery, record=delivery.record.model_copy(update={"content": "meet at noon"})
    )

    result = check(h, replace(transcript, deliveries=(forged,)))

    delivery_checks = [m for m in result.mismatches if m.check == "delivery"]
    assert [(m.event_id, m.detail) for m in delivery_checks] == [
        (delivery.event_id, "differs from what the send recorded")
    ]


async def test_a_delivery_after_a_drop_is_a_mismatch() -> None:
    h = await chat()
    transcript = transcript_of(h.store)
    (send,) = transcript.sends
    dropped = Recorded(
        "evt_drop",
        InterventionRecord(
            hook="before_deliver",
            action="drop",
            target_event_id=send.event_id,
            before_sha256=None,
            after={"recipient": "b", "kind": "drop", "reason": None},
        ),
    )

    result = check(h, replace(transcript, interventions=(dropped,)))

    (mismatch,) = result.mismatches
    assert mismatch.check == "delivery" and "dropped for this agent" in mismatch.detail


async def test_a_send_no_tool_call_or_post_explains_is_a_mismatch() -> None:
    h = await chat()
    transcript = transcript_of(h.store)
    (send,) = transcript.sends
    altered = replace(send, record=send.record.model_copy(update={"content": "bye"}))
    posted = replace(send, agent_id=None)

    altered_result = check(h, replace(transcript, sends=(altered,)))
    posted_result = check(h, replace(transcript, sends=(posted,)))

    assert [m.check for m in altered_result.mismatches] == ["send", "delivery"]
    assert "differs from what tool call" in altered_result.mismatches[0].detail
    assert [(m.check, m.detail) for m in posted_result.mismatches] == [
        ("send", "no extension post holds this message")
    ]
