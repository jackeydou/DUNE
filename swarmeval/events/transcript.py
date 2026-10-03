"""A run's two transcripts, read back from its rows for the transcript check
(docs/event-log.md#transcript-check): what the agents saw (`messages`) and what the sources
recorded (model calls with the gateway's record, tool results, interventions, sends, and
deliveries).

The rows are the evidence original, so the check reads them rather than the worker's memory.
"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from inspect_ai.event import Event, InfoEvent, ModelEvent, ToolEvent
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.db import events, messages
from swarmeval.events.convert import from_inspect_assistant
from swarmeval.runtime.messages import AssistantMessage, ChatMessage, RequestOptions
from swarmeval.runtime.records import (
    GatewayRecord,
    InterventionRecord,
    MessageDeliverRecord,
    MessageSendRecord,
    ToolResult,
)

_EVENT = TypeAdapter[Event](Event)
_MESSAGE = TypeAdapter[ChatMessage](ChatMessage)


@dataclass(frozen=True)
class ModelCall:
    event_id: str
    agent_id: str | None
    model: str
    gen: int | None
    length: int | None
    """The call was built from the first `length` messages of generation `gen`."""
    options: RequestOptions
    response: AssistantMessage
    gateway: GatewayRecord


@dataclass(frozen=True)
class ToolOutcome:
    event_id: str
    agent_id: str | None
    parent_id: str | None
    """The model event whose call this was."""
    result: ToolResult
    executed_arguments: str | None = None
    """What actually ran, after any `before_tool_call` rewrite; `None` when nothing ran."""


@dataclass(frozen=True)
class Sent:
    event_id: str
    seq: int
    agent_id: str | None
    """`None` for a message an extension posted."""
    parent_id: str | None
    record: MessageSendRecord


@dataclass(frozen=True)
class Recorded[R]:
    event_id: str
    record: R


@dataclass(frozen=True)
class RunTranscript:
    """Everything in `seq` order."""

    model_calls: tuple[ModelCall, ...]
    tool_results: tuple[ToolOutcome, ...]
    interventions: tuple[Recorded[InterventionRecord], ...]
    sends: tuple[Sent, ...]
    deliveries: tuple[Recorded[MessageDeliverRecord], ...]
    contexts: Mapping[tuple[str, int], tuple[ChatMessage, ...]]
    """`(agent_id, gen)` to that generation's messages, in `idx` order."""


async def load_transcript(engine: AsyncEngine, run_id: str) -> RunTranscript:
    model_calls: list[ModelCall] = []
    tool_results: list[ToolOutcome] = []
    interventions: list[Recorded[InterventionRecord]] = []
    sends: list[Sent] = []
    deliveries: list[Recorded[MessageDeliverRecord]] = []
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(
                events.c.event_id,
                events.c.seq,
                events.c.agent_id,
                events.c.parent_id,
                events.c.payload,
            )
            .where(
                events.c.run_id == run_id,
                events.c.type.in_(
                    (
                        "model",
                        "tool",
                        "swarmeval.intervention",
                        "swarmeval.msg.send",
                        "swarmeval.msg.deliver",
                    )
                ),
            )
            .order_by(events.c.seq)
        )
        for event_id, seq, agent_id, parent_id, payload in rows:
            match _EVENT.validate_python(payload):
                case ModelEvent() as event:
                    model_calls.append(_model_call(event_id, agent_id, event))
                case ToolEvent() as event:
                    result = ToolResult(
                        call_id=event.id,
                        tool=event.function,
                        content=str(event.result),
                        is_error=bool(event.failed),
                    )
                    assert event.metadata is not None, "stored events carry metadata.swarmeval"
                    executed = event.metadata["swarmeval"]["executed_arguments"]
                    tool_results.append(
                        ToolOutcome(event_id, agent_id, parent_id, result, executed)
                    )
                case InfoEvent(source="swarmeval.intervention", data=data):
                    record = InterventionRecord.model_validate(data)
                    interventions.append(Recorded(event_id, record))
                case InfoEvent(source="swarmeval.msg.send", data=data):
                    send = MessageSendRecord.model_validate(data)
                    sends.append(Sent(event_id, seq, agent_id, parent_id, send))
                case InfoEvent(data=data):
                    deliveries.append(Recorded(event_id, MessageDeliverRecord.model_validate(data)))
                case other:
                    raise AssertionError(f"selected by type, got {other.event}")
        stored = await conn.execute(
            select(messages.c.agent_id, messages.c.gen, messages.c.message)
            .where(messages.c.run_id == run_id)
            .order_by(messages.c.agent_id, messages.c.gen, messages.c.idx)
        )
        contexts: dict[tuple[str, int], list[ChatMessage]] = defaultdict(list)
        for agent_id, gen, message in stored:
            contexts[(agent_id, gen)].append(_MESSAGE.validate_python(message))
    return RunTranscript(
        model_calls=tuple(model_calls),
        tool_results=tuple(tool_results),
        interventions=tuple(interventions),
        sends=tuple(sends),
        deliveries=tuple(deliveries),
        contexts={k: tuple(v) for k, v in contexts.items()},
    )


def _model_call(event_id: str, agent_id: str | None, event: ModelEvent) -> ModelCall:
    assert event.metadata is not None, "every stored event carries metadata.swarmeval"
    ours = event.metadata["swarmeval"]
    config = event.config
    return ModelCall(
        event_id=event_id,
        agent_id=agent_id,
        model=event.model,
        gen=ours["input"]["gen"],
        length=ours["input"]["len"],
        options=RequestOptions(
            tools=tuple(ours["tools"]),
            temperature=config.temperature,
            top_p=config.top_p,
            max_output_tokens=config.max_tokens,
            seed=config.seed,
        ),
        response=from_inspect_assistant(
            event.output.choices[0].message, ours["raw_tool_arguments"]
        ),
        gateway=GatewayRecord.model_validate(ours["gateway"]),
    )
