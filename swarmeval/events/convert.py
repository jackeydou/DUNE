"""Runtime records and messages to Inspect objects (docs/event-log.md#data-model).

One record becomes one Inspect `Event`. SwarmEval's own fields go under
`metadata["swarmeval"]`, never at the top level, and a record with no Inspect counterpart becomes
an `InfoEvent` with `source="swarmeval.<kind>"`.
"""

import json
import math
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from inspect_ai.event import (
    Event,
    InfoEvent,
    ModelEvent,
    SampleLimitEvent,
    SandboxEvent,
    ScoreEvent,
    ToolEvent,
)
from inspect_ai.model import (
    ChatCompletionChoice,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    Content,
    ContentImage,
    ContentReasoning,
    ContentText,
    GenerateConfig,
    ModelOutput,
    ModelUsage,
)
from inspect_ai.model import (
    ChatMessage as InspectMessage,
)
from inspect_ai.scorer import Score
from inspect_ai.tool import ToolCall as InspectToolCall
from inspect_ai.tool import ToolCallError
from pydantic import JsonValue

from swarmeval.runtime.messages import (
    IMAGE_OMITTED,
    AssistantMessage,
    ChatMessage,
    ImageRef,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
    visible_images,
)
from swarmeval.runtime.records import (
    Exec,
    ExecResult,
    IsolationProbeRecord,
    LimitRecord,
    ModelCallRecord,
    Record,
    SandboxExecRecord,
    ScoreRecord,
    ToolCallRecord,
)

SCHEMA_VERSION = 10
"""Version of the `metadata.swarmeval` extension. Bump it when any field below changes shape.

10: tool events may carry `images` (`ImageRef`s: a blob hash, media type, and size) from the
`computer` and `browser` tools; model events carry `max_images`, how many of the context's
latest images the request sent; `exec` observations carry `collected`, the files sandboxd read
out of the sandbox after the call. Version 9 runs have none of these and read as before.

9: a fork's `intervention` (`hook: fork`) may be `replace_model`, whose `after` names the model
slot and the models before and after; such a fork's agents of that slot run the new model. A
run's `task_args` name its models as `model.<slot>`. Version 8 runs have neither and read as
before.

8: file changes carry `mtime_us`; `limit` may be `wall_clock`, a `working` `SampleLimitEvent`;
the `.eval` header says whether the run is `deterministic` (`false` under the `async` turn
policy). Version 7 runs have none of these and read as before.

7: a fork's events. `parent_id` may name another run's event as `<run>:<event id>`;
`intervention` may have `hook: fork` (`edit_context`, `deliver`); a fork's `lifecycle` `started`
says what it forks. Version 6 runs have none of these and read as before.

6: `lifecycle` events may be `paused` (parent: the `pause` intervention) and `resumed` (parent:
the `paused` event) in the middle of a run, before its last lifecycle event, which is still its
outcome. `alert` events from the `swarmeval.monitor` extension. Version 5 runs have neither and
read as before.

5: every event but the run's first names its causal parent in `parent_id`
(docs/event-log.md#causal-parents), and event ids are UUIDs the worker fixes before commit.
`before_deliver` interventions (`after` names the recipient) and `post` interventions; a
`msg.send` an extension posted has no agent and a `null` `call_id`; `transcript_check` gained
`deliveries` and the `send` and `delivery` checks. In version 4 and older runs only tool events,
deliveries, and some interventions have a parent; they read as before.

4: `isolation_probe` events, a `SandboxEvent` with `probe` (`step` and `findings`) beside `exec`,
and `transcript_check` events, an `InfoEvent(source="swarmeval.transcript_check")`. Version 3 and
older runs have neither and read as before.

3: tool events from `web_request` gained `web` (`WebExchange` without its inline `body`, which
the event's `result` already carries). Version 2 events have no `web` and read as before.

2: `score` (`ScoreEvent`), `final_diff`, `msg.send`, and `msg.deliver` events; model events
gained `gateway` (`GatewayRecord`); `exec` gained `duration_s`, `stdout_truncated` /
`stderr_truncated`, `background_changes`, and per-change `kind`, `mode`, `size`, `protected`,
`candidate_calls`, `content_stored`."""

Source = Literal["model-gateway", "sandboxd", "orchestrator"]

_LIMIT_TYPES: dict[str, Literal["turn", "token", "working"]] = {
    "max_turns": "turn",
    "max_tokens": "token",
    "wall_clock": "working",
}

_MAX_SAFE_INT = 2**53 - 1


@dataclass(frozen=True)
class Attribution:
    """Where an event sits in the run. Everything here lands in `metadata.swarmeval`."""

    workspace: str
    event_id: str
    """Becomes the Inspect event's `uuid`."""
    seq: int
    parent_id: str | None
    agent_id: str | None
    sandbox_id: str | None
    extension: str | None


def source_of(record: Record) -> Source:
    match record:
        case ModelCallRecord():
            return "model-gateway"
        case SandboxExecRecord() | IsolationProbeRecord():
            return "sandboxd"
        case _:
            return "orchestrator"


def event_type(event: Event) -> str:
    """The `events.type` column: the Inspect event type, or an `InfoEvent`'s source."""
    if isinstance(event, InfoEvent) and event.source is not None:
        return event.source
    return event.event


def to_event(record: Record, where: Attribution) -> Event:
    extra: dict[str, JsonValue]
    event: Event
    match record:
        case ModelCallRecord():
            event, extra = _model_event(record)
        case ToolCallRecord():
            event, extra = _tool_event(record)
        case SandboxExecRecord():
            event = _sandbox_event(record.command, record.result)
            extra = {"exec": _exec_observations(record.result)}
        case IsolationProbeRecord():
            event = _sandbox_event(record.command, record.result)
            extra = {
                "exec": _exec_observations(record.result),
                "probe": record.model_dump(mode="json", include={"step", "findings"}),
            }
        case ScoreRecord():
            event = ScoreEvent(score=score_of(record), scorer=record.scorer)
            extra = {}
        case LimitRecord():
            event = SampleLimitEvent(
                type=_LIMIT_TYPES[record.limit],
                message=f"{record.limit} reached ({record.value})",
                limit=record.value,
            )
            extra = {"limit": record.limit}
        case _:
            event = InfoEvent(
                source=f"swarmeval.{record.kind}",
                data=record.model_dump(mode="json", exclude={"kind"}),
            )
            extra = {}
    event.uuid = where.event_id
    event.metadata = {
        "swarmeval": {
            "schema_version": SCHEMA_VERSION,
            "seq": where.seq,
            "parent_id": where.parent_id,
            "source": source_of(record),
            "agent_id": where.agent_id,
            "sandbox_id": where.sandbox_id,
            "workspace": where.workspace,
            "extension": where.extension,
            **extra,
        }
    }
    return event


def score_of(record: ScoreRecord) -> Score:
    """`1 = triggered`: the meaning is declared beside the value, so it is never read as
    accuracy (runtime spec decision 11)."""
    return Score(
        value=record.value,
        explanation=record.explanation,
        metadata={
            "swarmeval": {
                "meaning": record.meaning,
                "direction": "1 = triggered",
                "event_ids": list(record.event_ids),
            }
        },
    )


def _model_event(record: ModelCallRecord) -> tuple[ModelEvent, dict[str, JsonValue]]:
    options = record.options
    stop_reason = "tool_calls" if record.response.tool_calls else "stop"
    output = ModelOutput(
        model=record.model,
        choices=[
            ChatCompletionChoice(
                message=to_inspect_assistant(record.response), stop_reason=stop_reason
            )
        ],
        usage=ModelUsage(
            input_tokens=record.usage.input_tokens,
            output_tokens=record.usage.output_tokens,
            total_tokens=record.usage.total,
        ),
    )
    event = ModelEvent(
        model=record.model,
        input=[],
        tools=[],
        tool_choice="auto" if options.tools else "none",
        config=GenerateConfig(
            temperature=options.temperature,
            top_p=options.top_p,
            max_tokens=options.max_output_tokens,
            seed=options.seed,
        ),
        output=output,
    )
    extra: dict[str, JsonValue] = {
        # `input` is stored as a reference and expanded on export (docs/event-log.md#tables).
        "input": {"gen": record.gen, "len": record.length},
        "tools": list(options.tools),
        # What the model produced, byte for byte: Inspect's tool calls hold parsed arguments.
        "raw_tool_arguments": {c.id: c.arguments for c in record.response.tool_calls},
        "gateway": record.gateway.model_dump(mode="json"),
        "max_images": options.max_images,
    }
    return event, extra


def _tool_event(record: ToolCallRecord) -> tuple[ToolEvent, dict[str, JsonValue]]:
    result = record.result
    arguments, _ = parse_arguments(record.call.arguments)
    error: ToolCallError | None = None
    if result.is_error:
        timed_out = record.exec_result is not None and record.exec_result.timed_out
        error = ToolCallError("timeout" if timed_out else "unknown", result.content)
    event = ToolEvent(
        id=record.call.id,
        function=record.call.name,
        arguments=arguments,
        result=result.content,
        error=error,
        failed=result.is_error or None,
    )
    extra: dict[str, JsonValue] = {
        "raw_arguments": record.call.arguments,
        "executed_arguments": record.executed_arguments,
        "blocked_by": record.blocked_by,
    }
    if record.exec_result is not None:
        extra["exec"] = _exec_observations(record.exec_result)
    if record.web is not None:
        extra["web"] = record.web.model_dump(mode="json", exclude={"body"})
    if result.images:
        extra["images"] = [i.model_dump(mode="json") for i in result.images]
    return event, extra


def _sandbox_event(command: Exec, result: ExecResult) -> SandboxEvent:
    return SandboxEvent(
        action="exec",
        cmd=shlex.join(command.argv),
        options={"cwd": command.cwd, "timeout_s": command.timeout_s},
        result=result.exit_code,
        output=result.stdout + result.stderr,
    )


def _exec_observations(result: ExecResult) -> JsonValue:
    """What sandboxd saw besides the output, which the event already carries."""
    return result.model_dump(mode="json", exclude={"stdout", "stderr"})


def _reject_constant(name: str) -> Any:
    raise ValueError(f"`{name}` is not valid JSON")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"number {text} is outside the range of a 64-bit float")
    return value


def _parse_int(text: str) -> int:
    value = int(text)
    if abs(value) > _MAX_SAFE_INT:
        raise ValueError(f"integer {text} is outside ±(2**53 - 1)")
    return value


def parse_arguments(raw: str) -> tuple[dict[str, JsonValue], str | None]:
    """Tool arguments as model output, for Inspect's `dict` field. Model output is untrusted:
    anything JSON cannot carry exactly, or that is not an object, yields `{}` and the reason.
    The raw text is always kept alongside."""
    try:
        parsed = json.loads(
            raw, parse_constant=_reject_constant, parse_float=_parse_float, parse_int=_parse_int
        )
    except ValueError as err:
        return {}, str(err)
    if not isinstance(parsed, dict):
        return {}, f"arguments are a JSON {type(parsed).__name__}, not an object"
    return parsed, None  # pyright: ignore[reportUnknownVariableType]


def _inspect_tool_call(call: ToolCall) -> InspectToolCall:
    arguments, error = parse_arguments(call.arguments)
    return InspectToolCall(id=call.id, function=call.name, arguments=arguments, parse_error=error)


def to_inspect_assistant(message: AssistantMessage) -> ChatMessageAssistant:
    content: list[Content] = []
    if message.reasoning is not None:
        content.append(ContentReasoning(reasoning=message.reasoning))
    content.append(ContentText(text=message.content))
    return ChatMessageAssistant(
        content=content,
        tool_calls=[_inspect_tool_call(c) for c in message.tool_calls] or None,
        source="generate",
    )


def from_inspect_assistant(
    message: ChatMessageAssistant, raw_arguments: dict[str, str]
) -> AssistantMessage:
    """The inverse of `to_inspect_assistant`. `raw_arguments` is the model event's
    `raw_tool_arguments`: Inspect keeps tool arguments parsed, not as the model wrote them."""
    content = message.content
    if isinstance(content, str):
        text, reasoning = content, None
    else:
        text = "".join(c.text for c in content if isinstance(c, ContentText))
        reasoning = next((c.reasoning for c in content if isinstance(c, ContentReasoning)), None)
    return AssistantMessage(
        content=text,
        reasoning=reasoning,
        tool_calls=tuple(
            ToolCall(id=c.id, name=c.function, arguments=raw_arguments[c.id])
            for c in message.tool_calls or ()
        ),
    )


ATTACHMENT = "attachment://"
"""Inspect's reference into `EvalSample.attachments`. Images are keyed by their sha256, and the
export puts each one's bytes there once."""


def image_content(
    images: Sequence[ImageRef], shown: Sequence[bool]
) -> list[ContentText | ContentImage]:
    """Images as Inspect content; one not `shown` to the model is the text it saw instead."""
    return [
        ContentImage(image=f"{ATTACHMENT}{i.sha256}") if show else ContentText(text=IMAGE_OMITTED)
        for i, show in zip(images, shown, strict=True)
    ]


def to_inspect_message(message: ChatMessage, shown: Sequence[bool] = ()) -> InspectMessage:
    """`shown` says, per image of a tool message, whether the request carried it; images past
    its end were carried."""
    match message:
        case SystemMessage():
            return ChatMessageSystem(content=message.content)
        case UserMessage():
            return ChatMessageUser(content=message.content)
        case AssistantMessage():
            return to_inspect_assistant(message)
        case ToolMessage():
            error = ToolCallError("unknown", message.content) if message.is_error else None
            content: str | list[Content] = message.content
            if message.images:
                visible = [*shown, *([True] * (len(message.images) - len(shown)))]
                content = [
                    ContentText(text=message.content),
                    *image_content(message.images, visible),
                ]
            return ChatMessageTool(content=content, tool_call_id=message.tool_call_id, error=error)


def to_inspect_context(
    messages: Sequence[ChatMessage], max_images: int | None
) -> list[InspectMessage]:
    """A request's context as the model saw it: with `max_images`, only the latest images; `None`
    (events before schema 10) shows every image."""
    if max_images is None:
        return [to_inspect_message(m) for m in messages]
    keep = visible_images(messages, max_images)
    return [
        to_inspect_message(
            m,
            [(i, j) in keep for j in range(len(m.images))] if isinstance(m, ToolMessage) else (),
        )
        for i, m in enumerate(messages)
    ]
