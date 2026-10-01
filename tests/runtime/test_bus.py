from swarmeval.gateway.bus import ChannelSpec
from swarmeval.runtime.messages import ToolMessage, UserMessage
from swarmeval.runtime.records import MessageDeliverRecord, MessageSendRecord, ToolCallRecord
from tests.runtime.fakes import agent, call, harness, reply

TEAM = (ChannelSpec(id="team", members=("dev", "qa")),)
SEND = '{"channel": "team", "content": "tests pass?"}'


async def test_a_message_reaches_the_recipient_at_its_next_turn() -> None:
    h = harness(
        (agent("dev", tools=("send_message",)), agent("qa", tools=("send_message",))),
        {
            "dev": [reply("", call("send_message", SEND)), reply("done")],
            "qa": [reply("thinking"), reply("yes")],
        },
        channels=TEAM,
    )

    outcome = await h.loop.run()

    assert outcome.status == "finished"
    (send,) = h.store.records("msg.send")
    assert send.agent_id == "dev"
    assert isinstance(send.record, MessageSendRecord)
    assert (send.record.channel, send.record.recipients) == ("team", ("qa",))
    (deliver,) = h.store.records("msg.deliver")
    assert (deliver.agent_id, deliver.parent_id) == ("qa", send.event_id)
    assert isinstance(deliver.record, MessageDeliverRecord)
    assert (deliver.record.send_seq, deliver.record.content) == (send.seq, "tests pass?")
    qa_context = h.store.messages("qa")
    delivered = qa_context[[m.role for m in qa_context].index("user", 2)]
    assert isinstance(delivered, UserMessage)
    assert delivered.content == "Message from dev on channel `team`:\n\ntests pass?"
    tool_message = h.store.messages("dev")[3]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.content == "Sent to qa."


async def test_the_send_commits_with_its_tool_call() -> None:
    h = harness(
        (agent("dev", tools=("send_message",)), agent("qa", tools=())),
        {"dev": [reply("", call("send_message", SEND)), reply("done")], "qa": [reply("ok")] * 2},
        channels=TEAM,
    )

    await h.loop.run()

    (txn,) = [t for t in h.store.log if any(e.record.kind == "msg.send" for e in t.events)]
    assert [e.record.kind for e in txn.events] == ["msg.send", "tool"]


async def test_a_channel_the_sender_is_not_in_is_refused() -> None:
    channels = (*TEAM, ChannelSpec(id="ops", members=("qa", "ops")))
    h = harness(
        (agent("dev", tools=("send_message",)), agent("qa", tools=()), agent("ops", tools=())),
        {
            "dev": [
                reply("", call("send_message", '{"channel": "ops", "content": "x"}')),
                reply("ok"),
            ],
            "qa": [reply("ok")],
            "ops": [reply("ok")],
        },
        channels=channels,
    )

    await h.loop.run()

    assert h.store.records("msg.send") == []
    record = h.store.records("tool")[0].record
    assert isinstance(record, ToolCallRecord)
    assert record.result.is_error
    assert record.result.content == "You cannot send on channel `ops`. Your channels: team."


async def test_mail_wakes_a_finished_agent() -> None:
    h = harness(
        (agent("dev", tools=()), agent("qa", tools=("send_message",))),
        {
            "dev": [reply("done"), reply("fixed it")],
            "qa": [
                reply("", call("send_message", '{"channel": "team", "content": "bug!"}')),
                reply("ok"),
            ],
        },
        channels=TEAM,
    )

    outcome = await h.loop.run()

    assert outcome.status == "finished"
    assert outcome.turns == 4
    dev_roles = [m.role for m in h.store.messages("dev")]
    assert dev_roles == ["system", "user", "assistant", "user", "assistant"]
    assert h.store.agent_states[-1].status == "finished"
