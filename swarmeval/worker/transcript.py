"""The transcript check: at run end, what each agent saw against what the sources recorded
(docs/event-log.md#transcript-check, v1 spec §6).

Every message in an agent's context must come from a recorded event, as is or as an
intervention changed it; every model call's request must hash, rebuilt from the stored context,
to what model-gateway received; every response must be what the backend's raw response
normalizes to; every message sent must come from a `send_message` call or an extension's post;
and every delivery must carry what was sent, or what a `before_deliver` rewrite made of it. A
difference an intervention explains is an intervention. Any other is a mismatch: the context
and the evidence have split, which is how spoofing shows.
"""

import hashlib
import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, TypeAdapter

from swarmeval.events.transcript import ModelCall, Recorded, RunTranscript, Sent, ToolOutcome
from swarmeval.gateway.bus import delivered_message
from swarmeval.gateway.model.client import from_wire, to_wire
from swarmeval.gateway.model.upstream import normalize_raw
from swarmeval.runtime.extensions.api import Inject
from swarmeval.runtime.messages import (
    AssistantMessage,
    ChatMessage,
    ModelRequest,
    SystemMessage,
    ToolMessage,
    ToolSchema,
    UserMessage,
)
from swarmeval.runtime.records import (
    InterventionRecord,
    MessageDeliverRecord,
    ToolResult,
    TranscriptCheckRecord,
    TranscriptMismatch,
)

_CONTEXT = TypeAdapter[tuple[ChatMessage, ...]](tuple[ChatMessage, ...])


class _Injected(BaseModel):
    """`after` of an injection through `ctx.actions.inject`."""

    agent_id: str
    content: str


@dataclass
class _Sources:
    """What may explain a context message, indexed for the walk."""

    calls: dict[tuple[str, int, int], ModelCall] = field(
        default_factory=dict[tuple[str, int, int], ModelCall]
    )
    """By (agent, gen, length of the context the call was built from): the call's response is
    admitted at exactly that index."""
    tools: dict[str, list[ToolOutcome]] = field(default_factory=dict[str, list[ToolOutcome]])
    """By the model event that made the calls, in `seq` order."""
    rewrites: dict[str, Recorded[InterventionRecord]] = field(
        default_factory=dict[str, Recorded[InterventionRecord]]
    )
    """The last `after_model_response` / `after_tool_result` rewrite of each event."""
    compactions: list[tuple[str, tuple[ChatMessage, ...]]] = field(
        default_factory=list[tuple[str, tuple[ChatMessage, ...]]]
    )
    mail: dict[str, list[tuple[str, UserMessage]]] = field(
        default_factory=dict[str, list[tuple[str, UserMessage]]]
    )
    """Per agent: deliveries and `ctx.actions.inject` injections, admitted at a turn's start."""
    deliveries: set[str] = field(default_factory=set[str])
    """The `msg.deliver` events in `mail`: sources, but not interventions."""
    turn_injections: list[tuple[str, UserMessage]] = field(
        default_factory=list[tuple[str, UserMessage]]
    )
    """`before_turn` returning `Inject`. Its intervention names no agent, so it may explain an
    equal message in any agent's context."""


def transcript_images(transcript: RunTranscript) -> set[str]:
    """Every image the run's contexts hold, which `check_transcript` needs the bytes of."""
    return {
        image.sha256
        for context in transcript.contexts.values()
        for message in context
        if isinstance(message, ToolMessage)
        for image in message.images
    }


def check_transcript(
    transcript: RunTranscript,
    *,
    prompts: Mapping[str, tuple[ChatMessage, ...]],
    tools: Mapping[str, ToolSchema],
    images: Mapping[str, bytes] | None = None,
) -> TranscriptCheckRecord:
    """`prompts` is each agent's generation 0 before its first turn; `tools` every tool the run
    could offer, by name, as requests carried them; `images` the bytes of every image
    `transcript_images` names, which requests carried inline."""
    sources = _index(transcript)
    mismatches: list[TranscriptMismatch] = []
    explained: list[str] = []
    compared = 0
    for (agent_id, gen), context in sorted(transcript.contexts.items()):
        compared += len(context)
        mismatches.extend(_walk(agent_id, gen, context, prompts[agent_id], sources, explained))
    requests = 0
    inherited = transcript.inherited
    for call in transcript.model_calls:
        if call.agent_id is None or call.gen is None or call.length is None:
            continue
        if call.event_id in inherited:
            continue
        requests += 1
        mismatch = _check_request(
            call,
            call.agent_id,
            call.gen,
            call.length,
            transcript,
            tools,
            {} if images is None else images,
        )
        if mismatch is not None:
            mismatches.append(mismatch)
    mismatches.extend(
        m
        for c in transcript.model_calls
        if c.event_id not in inherited and (m := _check_response(c)) is not None
    )
    mismatches.extend(_check_sends(transcript, explained))
    mismatches.extend(_check_deliveries(transcript, explained))
    return TranscriptCheckRecord(
        consistent=not mismatches,
        messages=compared,
        requests=requests,
        responses=len(transcript.model_calls),
        deliveries=len(transcript.deliveries),
        interventions=tuple(dict.fromkeys(explained)),
        mismatches=tuple(mismatches),
        event_ids=tuple(dict.fromkeys(m.event_id for m in mismatches if m.event_id)),
    )


def _index(transcript: RunTranscript) -> _Sources:
    sources = _Sources()
    for call in transcript.model_calls:
        if call.agent_id is not None and call.gen is not None and call.length is not None:
            sources.calls[(call.agent_id, call.gen, call.length)] = call
    for outcome in transcript.tool_results:
        if outcome.parent_id is not None:
            sources.tools.setdefault(outcome.parent_id, []).append(outcome)
    for d in transcript.deliveries:
        sources.mail.setdefault(d.record.recipient, []).append(
            (d.event_id, delivered_message(d.record))
        )
        sources.deliveries.add(d.event_id)
    for i in transcript.interventions:
        record = i.record
        match (record.hook, record.action):
            case ("after_model_response" | "after_tool_result", "rewrite"):
                assert record.target_event_id is not None, "transforms name what they changed"
                sources.rewrites[record.target_event_id] = i
            case ("compact_context", "compact") | ("fork", "edit_context"):
                sources.compactions.append((i.event_id, _CONTEXT.validate_python(record.after)))
            case (_, "inject") if isinstance(record.after, dict) and "messages" in record.after:
                decision = Inject.model_validate(record.after)
                sources.turn_injections.extend((i.event_id, m) for m in decision.messages)
            case (_, "inject"):
                injected = _Injected.model_validate(record.after)
                sources.mail.setdefault(injected.agent_id, []).append(
                    (i.event_id, UserMessage(content=injected.content))
                )
            case _:
                pass
    return sources


def _walk(
    agent_id: str,
    gen: int,
    context: Sequence[ChatMessage],
    prompt: tuple[ChatMessage, ...],
    sources: _Sources,
    explained: list[str],
) -> list[TranscriptMismatch]:
    def mismatch(idx: int, event_id: str | None, detail: str) -> TranscriptMismatch:
        return TranscriptMismatch(
            check="context", agent_id=agent_id, gen=gen, idx=idx, event_id=event_id, detail=detail
        )

    if gen == 0:
        start = prompt
        if tuple(context[: len(start)]) != start:
            return [mismatch(0, None, "generation 0 does not start with the case's prompts")]
    else:
        compaction = _take(sources.compactions, lambda c: tuple(context[: len(c[1])]) == c[1])
        if compaction is None:
            return [
                mismatch(
                    0,
                    None,
                    f"generation {gen} starts with messages no compact_context intervention "
                    "produced; the rest of it is not checked",
                )
            ]
        explained.append(compaction[0])
        start = compaction[1]
    found: list[TranscriptMismatch] = []
    pending: deque[ToolOutcome] = deque()
    for idx in range(len(start), len(context)):
        message = context[idx]
        match message:
            case AssistantMessage():
                call = sources.calls.get((agent_id, gen, idx))
                pending = deque(sources.tools.get(call.event_id, ()) if call else ())
                if call is None:
                    found.append(mismatch(idx, None, "no model call answered this context"))
                    continue
                expected, by = _rewritten(call.event_id, call.response, sources, AssistantMessage)
                if message != expected:
                    found.append(mismatch(idx, call.event_id, _differs("model call", by)))
                elif by is not None:
                    explained.append(by)
            case ToolMessage():
                if not pending:
                    found.append(
                        mismatch(idx, None, "no tool call of the preceding model call is left")
                    )
                    continue
                outcome = pending.popleft()
                result, by = _rewritten(outcome.event_id, outcome.result, sources, ToolResult)
                expected = ToolMessage(
                    tool_call_id=outcome.result.call_id,
                    content=result.content,
                    is_error=result.is_error,
                    images=result.images,
                )
                if message != expected:
                    found.append(mismatch(idx, outcome.event_id, _differs("tool call", by)))
                elif by is not None:
                    explained.append(by)
            case UserMessage():
                source = _take_equal(sources.mail.get(agent_id, []), message)
                source = source or _take_equal(sources.turn_injections, message)
                if source is None:
                    found.append(mismatch(idx, None, "no delivery or injection holds this message"))
                elif source[0] not in sources.deliveries:
                    explained.append(source[0])
            case SystemMessage():
                found.append(mismatch(idx, None, "a system message after the generation's start"))
    return found


def _take[T](items: list[T], matches: Callable[[T], bool]) -> T | None:
    """Removes and returns the first item that matches: each source explains one message."""
    for i, item in enumerate(items):
        if matches(item):
            return items.pop(i)
    return None


def _take_equal[T](items: list[tuple[str, T]], value: T) -> tuple[str, T] | None:
    return _take(items, lambda item: item[1] == value)


def _rewritten[M: BaseModel](
    event_id: str, original: M, sources: _Sources, model: type[M]
) -> tuple[M, str | None]:
    """What the agent should have been shown for `event_id`, and the intervention that changed
    it, if one did."""
    rewrite = sources.rewrites.get(event_id)
    if rewrite is None:
        return original, None
    return model.model_validate(rewrite.record.after), rewrite.event_id


def _differs(source: str, by: str | None) -> str:
    if by is None:
        return f"differs from what the {source} recorded"
    return f"differs from what intervention {by} made of what the {source} recorded"


def _check_request(
    call: ModelCall,
    agent_id: str,
    gen: int,
    length: int,
    transcript: RunTranscript,
    tools: Mapping[str, ToolSchema],
    images: Mapping[str, bytes],
) -> TranscriptMismatch | None:
    def mismatch(detail: str) -> TranscriptMismatch:
        return TranscriptMismatch(
            check="request",
            agent_id=agent_id,
            gen=gen,
            idx=length,
            event_id=call.event_id,
            detail=detail,
        )

    context = transcript.contexts.get((agent_id, gen), ())
    if len(context) < length:
        return mismatch(
            f"built from {length} messages of generation {gen}, which holds {len(context)}"
        )
    unknown = [name for name in call.options.tools if name not in tools]
    if unknown:
        return mismatch(f"offered tools this run does not have: {', '.join(unknown)}")
    request = ModelRequest(
        model=call.model,
        messages=tuple(context[:length]),
        gen=gen,
        tools=tuple(tools[name] for name in call.options.tools),
        options=call.options,
    )
    body = to_wire(request, images).model_dump_json(exclude_none=True).encode()
    digest = hashlib.sha256(body).hexdigest()
    if digest != call.gateway.request_sha256:
        return mismatch(
            f"the request rebuilt from the stored context hashes to {digest}; model-gateway "
            f"received {call.gateway.request_sha256}"
        )
    return None


def _check_response(call: ModelCall) -> TranscriptMismatch | None:
    def mismatch(detail: str) -> TranscriptMismatch:
        return TranscriptMismatch(
            check="response",
            agent_id=call.agent_id,
            gen=call.gen,
            idx=call.length,
            event_id=call.event_id,
            detail=detail,
        )

    try:
        raw = from_wire(normalize_raw(call.gateway.upstream_response_json, call.model)).message
    except ValueError as err:
        return mismatch(f"the backend's raw response does not read as a completion: {err}")
    if raw != call.response:
        return mismatch("the recorded response differs from the backend's raw response")
    return None


class _Posted(BaseModel):
    """`after` of a post through `ctx.actions.post`."""

    channel: str
    sender: str
    content: str


def _check_sends(transcript: RunTranscript, explained: list[str]) -> list[TranscriptMismatch]:
    """An agent's send must be what its `send_message` call ran with; an extension's, what its
    post intervention (the send's parent) says."""
    tools: dict[tuple[str | None, str | None], ToolOutcome] = {
        (t.parent_id, t.result.call_id): t for t in transcript.tool_results
    }
    posts = {
        i.event_id: i.record
        for i in transcript.interventions
        if i.record.action == "post" and isinstance(i.record.after, dict)
    }
    found: list[TranscriptMismatch] = []
    for send in transcript.sends:
        if send.event_id in transcript.inherited:
            continue
        record = send.record
        if send.agent_id is None:
            post = posts.get(send.parent_id or "")
            claimed = _Posted(channel=record.channel, sender=record.sender, content=record.content)
            if post is None or _Posted.model_validate(post.after) != claimed:
                found.append(_send_mismatch(send, "no extension post holds this message"))
            else:
                explained.append(send.parent_id or "")
            continue
        tool = tools.get((send.parent_id, record.call_id))
        if tool is None or tool.result.tool != "send_message" or tool.executed_arguments is None:
            found.append(_send_mismatch(send, "no send_message call of its model event ran"))
            continue
        try:
            arguments = json.loads(tool.executed_arguments)
        except ValueError:
            arguments = None
        sent = (record.channel, record.content, record.sender)
        if (
            not isinstance(arguments, dict)
            or (
                arguments.get("channel"),  # pyright: ignore[reportUnknownMemberType]
                arguments.get("content"),  # pyright: ignore[reportUnknownMemberType]
                send.agent_id,
            )
            != sent
        ):
            found.append(_send_mismatch(send, f"differs from what tool call {tool.event_id} ran"))
    return found


def _send_mismatch(send: Sent, detail: str) -> TranscriptMismatch:
    return TranscriptMismatch(
        check="send",
        agent_id=send.agent_id,
        gen=None,
        idx=None,
        event_id=send.event_id,
        detail=detail,
    )


def _check_deliveries(transcript: RunTranscript, explained: list[str]) -> list[TranscriptMismatch]:
    """A delivery carries its send's content, or the last `before_deliver` rewrite of it for
    that recipient, and never follows a drop for that recipient. Every `before_deliver`
    intervention of a consistent run explains something: a delivery, or a recipient's not
    getting the message."""
    sends = {s.seq: s for s in transcript.sends}
    decided: dict[tuple[str, str], list[Recorded[InterventionRecord]]] = {}
    for i in transcript.interventions:
        delivery = i.record.hook == "before_deliver" or (
            i.record.hook == "fork" and i.record.action == "deliver"
        )
        if delivery and isinstance(i.record.after, dict):
            recipient = str(i.record.after["recipient"])
            key = (i.record.target_event_id or "", recipient)
            decided.setdefault(key, []).append(i)
    found: list[TranscriptMismatch] = []
    undelivered = dict(decided)
    for delivery in transcript.deliveries:
        if delivery.event_id in transcript.inherited:
            continue
        record = delivery.record
        send = sends.get(record.send_seq)
        if send is None:
            detail = f"delivers message {record.send_seq}, which no send recorded"
            found.append(_delivery_mismatch(delivery, detail))
            continue
        verdicts = undelivered.pop((send.event_id, record.recipient), [])
        if any(v.record.action == "drop" for v in verdicts):
            detail = f"delivers message {send.event_id}, dropped for this agent"
            found.append(_delivery_mismatch(delivery, detail))
            continue
        rewrites = [v for v in verdicts if v.record.action == "deliver"]
        expected = send.record.content
        if rewrites:
            assert isinstance(rewrites[-1].record.after, dict), "selected as a mapping above"
            expected = str(rewrites[-1].record.after["content"])
        sent = (send.record.channel, send.record.sender, expected)
        if (record.channel, record.sender, record.content) != sent:
            by = rewrites[-1].event_id if rewrites else None
            found.append(_delivery_mismatch(delivery, _differs("send", by)))
            continue
        explained.extend(v.event_id for v in verdicts)
    # A drop, or a hold the run ended inside, explains why a recipient never got the message.
    explained.extend(v.event_id for verdicts in undelivered.values() for v in verdicts)
    return found


def _delivery_mismatch(delivery: Recorded[MessageDeliverRecord], detail: str) -> TranscriptMismatch:
    return TranscriptMismatch(
        check="delivery",
        agent_id=delivery.record.recipient,
        gen=None,
        idx=None,
        event_id=delivery.event_id,
        detail=detail,
    )
