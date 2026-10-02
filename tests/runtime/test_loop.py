from dataclasses import replace

import pytest

from swarmeval.runtime import Limits, RunConfigError
from swarmeval.runtime.messages import AssistantMessage, ToolMessage
from swarmeval.runtime.ports import AgentCaller
from swarmeval.runtime.records import Exec, ExecResult, ToolCallRecord, WebExchange, WebRequest
from tests.runtime.fakes import FakeWeb, agent, call, harness, reply


async def test_agent_runs_a_tool_then_finishes() -> None:
    h = harness((agent(),), {"a": [reply("", call("shell", '{"cmd": "ls"}')), reply("done")]})

    outcome = await h.loop.run()

    assert (outcome.status, outcome.turns, outcome.tokens_used) == ("finished", 2, 20)
    assert h.sandbox.calls == [("box_a", None, Exec(argv=("sh", "-c", "ls")))]
    roles = [m.role for m in h.store.messages("a")]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    tool_message = h.store.messages("a")[3]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.content == "sh -c ls"
    kinds = [e.record.kind for e in h.store.events]
    assert kinds == ["lifecycle", "model", "tool", "model", "lifecycle"]
    model_event, tool_event = h.store.events[1], h.store.events[2]
    assert tool_event.parent_id == model_event.event_id
    assert h.store.agent_states[-1].status == "finished"


async def test_a_result_is_recorded_before_it_is_admitted() -> None:
    h = harness((agent(),), {"a": [reply("", call("shell", '{"cmd": "ls"}')), reply("done")]})

    await h.loop.run()

    recorded = next(
        i for i, t in enumerate(h.store.log) if any(e.record.kind == "tool" for e in t.events)
    )
    admitted = next(
        i
        for i, t in enumerate(h.store.log)
        if any(isinstance(m, ToolMessage) for _, m in t.messages)
    )
    assert recorded < admitted


async def test_invalid_arguments_become_an_error_result() -> None:
    h = harness((agent(),), {"a": [reply("", call("shell", '{"nope": 1}')), reply("done")]})

    await h.loop.run()

    assert h.sandbox.calls == []
    record = h.store.records("tool")[0].record
    assert isinstance(record, ToolCallRecord)
    assert record.executed_arguments is None
    assert record.result.is_error
    assert "Invalid arguments for `shell`" in record.result.content


async def test_a_tool_the_agent_does_not_have_is_an_error_result() -> None:
    h = harness((agent(),), {"a": [reply("", call("rm", "{}")), reply("done")]})

    await h.loop.run()

    message = h.store.messages("a")[3]
    assert isinstance(message, ToolMessage)
    assert message.is_error
    assert "Unknown tool `rm`" in message.content


async def test_nonzero_exit_is_reported_to_the_agent() -> None:
    h = harness((agent(),), {"a": [reply("", call("shell", '{"cmd": "false"}')), reply("done")]})
    h.sandbox.handler = lambda _: ExecResult(exit_code=2, stdout="", stderr="boom\n")

    await h.loop.run()

    message = h.store.messages("a")[3]
    assert isinstance(message, ToolMessage)
    assert message.is_error
    assert message.content == "boom\n\n[exit code 2]"


async def test_agents_take_turns_in_order() -> None:
    h = harness(
        (agent("a"), agent("b")),
        {
            "a": [reply("", call("shell", '{"cmd": "x"}')), reply("done")],
            "b": [reply("", call("shell", '{"cmd": "y"}')), reply("done")],
        },
    )

    await h.loop.run()

    callers = [c.agent_id for c, _ in h.model.requests if isinstance(c, AgentCaller)]
    assert callers == ["a", "b", "a", "b"]


async def test_max_turns_stops_the_run() -> None:
    looping = [reply("", call("shell", '{"cmd": "x"}')) for _ in range(10)]
    h = harness((agent(),), {"a": looping}, limits=Limits(max_turns=3))

    outcome = await h.loop.run()

    assert (outcome.status, outcome.turns) == ("limit", 3)
    assert [e.record.kind for e in h.store.events][-2:] == ["limit", "lifecycle"]


async def test_max_tokens_is_checked_before_each_model_call() -> None:
    looping = [reply("", call("shell", '{"cmd": "x"}'), tokens=60) for _ in range(10)]
    h = harness((agent(),), {"a": looping}, limits=Limits(max_tokens=100))

    outcome = await h.loop.run()

    assert (outcome.status, outcome.turns, outcome.tokens_used) == ("limit", 2, 120)


async def test_the_request_carries_the_admitted_context_and_tools() -> None:
    h = harness((agent(temperature=0.2),), {"a": [reply("done")]})

    await h.loop.run()

    _, request = h.model.requests[0]
    assert [m.role for m in request.messages] == ["system", "user"]
    assert [t.name for t in request.tools] == ["shell"]
    assert request.tools[0].parameters["required"] == ["cmd"]
    assert request.options.temperature == 0.2
    assert isinstance(h.store.messages("a")[-1], AssistantMessage)


def test_an_agent_listing_an_unknown_tool_is_rejected() -> None:
    with pytest.raises(RunConfigError, match="lists tool `browser`"):
        harness((agent(tools=("browser",)),), {})


def test_a_sandbox_tool_needs_a_sandbox() -> None:
    no_box = replace(agent(), sandbox_id=None)
    with pytest.raises(RunConfigError, match="has no sandbox"):
        harness((no_box,), {})


async def test_resuming_a_run_is_refused_for_now() -> None:
    first = harness((agent(),), {"a": [reply("done")]})
    await first.loop.run()
    second = harness((agent(),), {"a": [reply("done")]}, store=first.store)

    with pytest.raises(RunConfigError, match="Resuming a run is not supported yet"):
        await second.loop.run()


async def test_a_stop_from_outside_ends_the_run_at_the_next_hook_point() -> None:
    looping = [reply("", call("shell", '{"cmd": "x"}')) for _ in range(10)]
    h = harness((agent(),), {"a": looping})

    def handler(command: Exec) -> ExecResult:
        h.loop.stop("cancelled")
        return ExecResult(exit_code=0, stdout="", stderr="")

    h.sandbox.handler = handler

    outcome = await h.loop.run()

    assert (outcome.status, outcome.reason, outcome.turns) == ("stopped", "cancelled", 1)


async def test_web_request_runs_in_the_worker_and_its_exchange_is_recorded() -> None:
    web = FakeWeb()
    arguments = '{"url": "https://example.com/a", "headers": {"accept": "text/html"}}'
    h = harness(
        (agent(tools=("web_request",)),),
        {"a": [reply("", call("web_request", arguments)), reply("done")]},
        web=web,
    )

    await h.loop.run()

    assert h.sandbox.calls == []
    assert web.requests == [
        WebRequest(method="GET", url="https://example.com/a", headers=(("accept", "text/html"),))
    ]
    record = h.store.records("tool")[0].record
    assert isinstance(record, ToolCallRecord)
    assert record.web is not None and record.web.address == "93.184.215.14"
    assert record.executed_arguments == arguments
    message = h.store.messages("a")[3]
    assert isinstance(message, ToolMessage)
    assert message.content == "HTTP 200\n\nhttps://example.com/a"
    assert not message.is_error


async def test_a_refused_web_request_reads_as_a_refused_connection() -> None:
    web = FakeWeb(
        handler=lambda req: WebExchange(
            request=req, refused="metadata.internal resolves to non-public address 169.254.169.254"
        )
    )
    h = harness(
        (agent(tools=("web_request",)),),
        {"a": [reply("", call("web_request", '{"url": "http://metadata.internal/"}')), reply("")]},
        web=web,
    )

    await h.loop.run()

    message = h.store.messages("a")[3]
    assert isinstance(message, ToolMessage)
    assert message.is_error
    assert message.content == "Could not connect to http://metadata.internal/: connection refused."
    record = h.store.records("tool")[0].record
    assert isinstance(record, ToolCallRecord) and record.web is not None
    assert record.web.refused is not None and "169.254.169.254" in record.web.refused


async def test_web_request_headers_with_control_characters_are_invalid_arguments() -> None:
    web = FakeWeb()
    arguments = '{"url": "https://example.com/", "headers": {"x": "a\\r\\nHost: evil"}}'
    h = harness(
        (agent(tools=("web_request",)),),
        {"a": [reply("", call("web_request", arguments)), reply("done")]},
        web=web,
    )

    await h.loop.run()

    assert web.requests == []
    message = h.store.messages("a")[3]
    assert isinstance(message, ToolMessage)
    assert message.is_error and "Invalid arguments for `web_request`" in message.content


def test_web_request_needs_a_web_client() -> None:
    with pytest.raises(RunConfigError, match="web client"):
        harness((agent(tools=("web_request",)),), {"a": []})
