from typing import Any

import pytest
from inspect_ai.event import Event, InfoEvent, ModelEvent, SampleLimitEvent, SandboxEvent, ToolEvent
from inspect_ai.model import ChatMessageTool
from pydantic import TypeAdapter

from swarmeval.events import SCHEMA_VERSION, to_event, to_inspect_message
from swarmeval.events.convert import Attribution, parse_arguments
from swarmeval.runtime.messages import (
    AssistantMessage,
    RequestOptions,
    ToolCall,
    ToolMessage,
    Usage,
)
from swarmeval.runtime.records import (
    AlertRecord,
    Exec,
    ExecResult,
    FsChange,
    LimitRecord,
    ModelCallRecord,
    Record,
    SandboxExecRecord,
    ToolCallRecord,
    ToolResult,
)
from tests.runtime.fakes import GATEWAY

EVENT = TypeAdapter[Event](Event)
WHERE = Attribution(
    workspace="ws", seq=4, parent_id="evt_p", agent_id="a", sandbox_id="box_a", extension=None
)


def roundtrip(record: Record) -> Any:
    """Converts, then reads back the stored form the way an Inspect reader would."""
    event = to_event(record, WHERE)
    return EVENT.validate_python(event.model_dump(mode="json", exclude_none=True))


def test_every_event_carries_the_swarmeval_metadata() -> None:
    event = roundtrip(AlertRecord(message="hm", severity="low"))

    assert event.metadata == {
        "swarmeval": {
            "schema_version": SCHEMA_VERSION,
            "seq": 4,
            "parent_id": "evt_p",
            "source": "orchestrator",
            "agent_id": "a",
            "sandbox_id": "box_a",
            "workspace": "ws",
            "extension": None,
        }
    }


def test_a_record_without_an_inspect_type_is_an_info_event() -> None:
    event = roundtrip(AlertRecord(message="hm", severity="low", event_ids=("e1",)))

    assert isinstance(event, InfoEvent)
    assert event.source == "swarmeval.alert"
    assert event.data == {"message": "hm", "severity": "low", "event_ids": ["e1"]}


def test_a_model_call_is_a_model_event_with_input_by_reference() -> None:
    record = ModelCallRecord(
        model="m1",
        gen=0,
        length=3,
        options=RequestOptions(tools=("shell",), temperature=0.2, seed=5),
        response=AssistantMessage(
            content="ok",
            reasoning="think",
            tool_calls=(ToolCall(id="c1", name="shell", arguments='{"cmd": "ls"}'),),
        ),
        usage=Usage(input_tokens=7, output_tokens=3),
        gateway=GATEWAY,
    )

    event = roundtrip(record)

    assert isinstance(event, ModelEvent)
    assert event.input == []
    assert event.config.temperature == 0.2
    assert event.config.seed == 5
    assert event.output.usage is not None
    assert event.output.usage.total_tokens == 10
    choice = event.output.choices[0]
    assert choice.stop_reason == "tool_calls"
    assert choice.message.tool_calls is not None
    assert choice.message.tool_calls[0].arguments == {"cmd": "ls"}
    assert choice.message.text == "ok"
    assert event.metadata is not None
    ours = event.metadata["swarmeval"]
    assert ours["source"] == "model-gateway"
    assert ours["input"] == {"gen": 0, "len": 3}
    assert ours["tools"] == ["shell"]
    assert ours["raw_tool_arguments"] == {"c1": '{"cmd": "ls"}'}
    assert ours["gateway"]["upstream"]["backend"] == "scripted"


def test_a_tool_call_is_a_tool_event_with_sandbox_observations() -> None:
    change = FsChange(
        path="/workspace/a", op="create", uid=1000, before_sha256=None, after_sha256="ab"
    )
    record = ToolCallRecord(
        call=ToolCall(id="c1", name="shell", arguments='{"cmd": "false"}'),
        executed_arguments='{"cmd": "false"}',
        result=ToolResult(call_id="c1", tool="shell", content="\n[exit code 1]", is_error=True),
        exec_result=ExecResult(exit_code=1, stdout="", stderr="", fs_changes=(change,)),
    )

    event = roundtrip(record)

    assert isinstance(event, ToolEvent)
    assert (event.id, event.function, event.arguments) == ("c1", "shell", {"cmd": "false"})
    assert event.error is not None
    assert event.error.type == "unknown"
    assert event.failed is True
    assert event.metadata is not None
    exec_meta = event.metadata["swarmeval"]["exec"]
    assert exec_meta["exit_code"] == 1
    assert exec_meta["fs_changes"][0]["path"] == "/workspace/a"
    assert "stdout" not in exec_meta


def test_a_timed_out_tool_call_has_a_timeout_error() -> None:
    record = ToolCallRecord(
        call=ToolCall(id="c1", name="shell", arguments="{}"),
        executed_arguments="{}",
        result=ToolResult(call_id="c1", tool="shell", content="[timed out]", is_error=True),
        exec_result=ExecResult(exit_code=-1, stdout="", stderr="", timed_out=True),
    )

    event = roundtrip(record)

    assert isinstance(event, ToolEvent)
    assert event.error is not None
    assert event.error.type == "timeout"


def test_an_extension_exec_is_a_sandbox_event() -> None:
    record = SandboxExecRecord(
        sandbox_id="box_a",
        command=Exec(argv=("cat", "a b")),
        result=ExecResult(exit_code=0, stdout="hi", stderr=""),
    )

    event = roundtrip(record)

    assert isinstance(event, SandboxEvent)
    assert (event.action, event.cmd, event.result, event.output) == ("exec", "cat 'a b'", 0, "hi")
    assert event.metadata is not None
    assert event.metadata["swarmeval"]["source"] == "sandboxd"


def test_a_limit_is_a_sample_limit_event() -> None:
    event = roundtrip(LimitRecord(limit="max_tokens", value=100))

    assert isinstance(event, SampleLimitEvent)
    assert (event.type, event.limit) == ("token", 100)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("not json", "Expecting value"),
        ("[1, 2]", "JSON list, not an object"),
        ('{"x": NaN}', "`NaN` is not valid JSON"),
        ('{"x": 9007199254740993}', "outside ±(2**53 - 1)"),
        ('{"x": 1e400}', "outside the range of a 64-bit float"),
    ],
)
def test_arguments_json_cannot_carry_exactly_are_kept_raw(raw: str, reason: str) -> None:
    parsed, error = parse_arguments(raw)

    assert parsed == {}
    assert error is not None
    assert reason in error


def test_messages_convert_to_inspect_messages() -> None:
    tool = to_inspect_message(ToolMessage(tool_call_id="c1", content="boom", is_error=True))

    assert isinstance(tool, ChatMessageTool)
    assert tool.error is not None
    assert tool.text == "boom"
