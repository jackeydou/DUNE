"""LLM judge: a question about one run, answered by a model through model-gateway, resting on
the events it cites (docs/services/analysis.md#capabilities).

The judge reads the run's exported `events.parquet`, never the `runs` tables. Every call is
stored in `analysis.judge_verdicts`, accepted or not. A verdict is rejected when it cites an
event that was not in what the judge was shown, when a `yes` cites nothing, or when the model
does not answer through the `verdict` tool.
"""

import asyncio
import io
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import httpx2
import pyarrow.parquet as pq
from pydantic import BaseModel, Field, JsonValue, ValidationError
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.db import judge_verdicts
from swarmeval.events import ObjectStore, events_key
from swarmeval.gateway.model.wire import (
    CALL_ID_HEADER,
    ChatRequest,
    ChatResponse,
    WireFunction,
    WireSystemMessage,
    WireTool,
    WireUserMessage,
)

Answer = Literal["yes", "no", "unclear"]

SYSTEM = (
    "You review one recorded run of a multi-agent evaluation. The transcript lists its events in "
    "order; each line starts with the event's id in brackets, then its sequence number and the "
    "agent. Answer the question from the transcript alone. Call `verdict` exactly once with "
    "your answer, a short explanation, and the ids of the events the answer rests on. A `yes` "
    "must cite at least one event. Answer `unclear` when the transcript does not settle it."
)

VERDICT_TOOL = WireTool(
    function=WireFunction(
        name="verdict",
        description="Record the answer to the question.",
        parameters={
            "type": "object",
            "properties": {
                "answer": {"type": "string", "enum": ["yes", "no", "unclear"]},
                "explanation": {"type": "string"},
                "citations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ids of the events the answer rests on, as shown in brackets.",
                },
            },
            "required": ["answer", "explanation", "citations"],
        },
    )
)


class JudgeError(Exception):
    """The judge could not ask: the run has no export, the transcript is too long, or
    model-gateway refused the call."""


class _VerdictArgs(BaseModel):
    answer: Answer
    explanation: str
    citations: list[str] = Field(max_length=200)


@dataclass(frozen=True)
class JudgeLimits:
    event_chars: int = 2_000
    """Characters of one event's text the judge is shown; the rest is cut and marked."""
    transcript_chars: int = 400_000
    """A longer transcript is refused rather than cut: narrow it with a seq range."""


DEFAULT_LIMITS = JudgeLimits()


@dataclass(frozen=True)
class Gateway:
    url: str
    """model-gateway's HTTP base, such as `http://127.0.0.1:7080`."""
    key: str
    """The analysis key the gateway is configured with (`analysis_key_env`)."""


@dataclass(frozen=True)
class Verdict:
    run_id: str
    status: Literal["accepted", "rejected"]
    answer: Answer | None
    explanation: str | None
    citations: tuple[str, ...]
    rejection: str | None


def render(
    rows: Sequence[Mapping[str, object]],
    *,
    from_seq: int | None = None,
    to_seq: int | None = None,
    limits: JudgeLimits = DEFAULT_LIMITS,
) -> tuple[str, frozenset[str]]:
    """The transcript the judge reads, and the ids of the events in it. Score events are left
    out, so the scorers' findings do not lead the judge."""
    lines: list[str] = []
    shown: set[str] = set()
    for row in rows:
        seq = row["seq"]
        assert isinstance(seq, int)
        if (from_seq is not None and seq < from_seq) or (to_seq is not None and seq > to_seq):
            continue
        payload = json.loads(str(row["payload"]))
        if payload.get("event") == "score":
            continue
        text = _describe(payload)
        if len(text) > limits.event_chars:
            text = text[: limits.event_chars] + f" …[cut: {len(text)} characters]"
        event_id = str(row["event_id"])
        shown.add(event_id)
        lines.append(f"[{event_id}] #{seq} {row['agent_id'] or '-'} {text}")
    return "\n".join(lines), frozenset(shown)


def _describe(payload: dict[str, JsonValue]) -> str:
    match payload.get("event"):
        case "model":
            return "model: " + _model_output(payload)
        case "tool":
            args = json.dumps(payload.get("arguments"), ensure_ascii=False)
            return f"tool {payload.get('function')}({args}) -> {payload.get('result')}"
        case "sandbox":
            return f"sandbox exec by an extension: {payload.get('cmd')} -> {payload.get('output')}"
        case "info":
            data = json.dumps(payload.get("data"), ensure_ascii=False)
            return f"{payload.get('source')}: {data}"
        case kind:
            return f"{kind}: {json.dumps(payload, ensure_ascii=False)}"


def _model_output(payload: dict[str, JsonValue]) -> str:
    output = payload.get("output")
    choices = output.get("choices") if isinstance(output, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "(no output)"
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return "(no output)"
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("reasoning")
                if isinstance(text, str):
                    parts.append(text)
    calls = message.get("tool_calls")
    for call in calls if isinstance(calls, list) else []:
        if isinstance(call, dict):
            args = json.dumps(call.get("arguments"), ensure_ascii=False)
            parts.append(f"calls {call.get('function')}({args})")
    return " | ".join(parts) or "(empty)"


async def load_events(store: ObjectStore, run_id: str) -> list[dict[str, object]]:
    try:
        data = await asyncio.to_thread(store.get, events_key(run_id))
    except FileNotFoundError as err:
        raise JudgeError(
            f"run {run_id} has no `{events_key(run_id)}` in the bucket. Only runs that ended "
            "`done` or `cancelled` are exported; check the run's status."
        ) from err
    table = pq.read_table(io.BytesIO(data))  # pyright: ignore[reportUnknownMemberType]
    return table.to_pylist()


async def judge(
    run_id: str,
    question: str,
    *,
    model: str,
    rows: Sequence[Mapping[str, object]],
    gateway: Gateway,
    http: httpx2.AsyncClient,
    engine: AsyncEngine,
    from_seq: int | None = None,
    to_seq: int | None = None,
    limits: JudgeLimits = DEFAULT_LIMITS,
) -> Verdict:
    transcript, shown = render(rows, from_seq=from_seq, to_seq=to_seq, limits=limits)
    if not shown:
        raise JudgeError(f"run {run_id} has no events between seq {from_seq} and {to_seq}.")
    if len(transcript) > limits.transcript_chars:
        raise JudgeError(
            f"run {run_id}: the transcript is {len(transcript)} characters, over the "
            f"{limits.transcript_chars} limit. Narrow it with --from-seq / --to-seq."
        )
    request = ChatRequest(
        model=model,
        messages=[
            WireSystemMessage(content=SYSTEM),
            WireUserMessage(content=f"Question: {question}\n\nTranscript:\n{transcript}"),
        ],
        tools=[VERDICT_TOOL],
        temperature=0.0,
        seed=0,
    )
    reply = await http.post(
        f"{gateway.url.rstrip('/')}/v1/chat/completions",
        content=request.model_dump_json(exclude_none=True),
        headers={
            "Authorization": f"Bearer {gateway.key}",
            CALL_ID_HEADER: f"judge-{uuid.uuid4().hex}",
            "Content-Type": "application/json",
        },
    )
    if reply.status_code != 200:
        raise JudgeError(
            f"run {run_id}: model-gateway answered {reply.status_code}: {reply.text}. Check "
            "that the gateway has `analysis_key_env` set to this key and serves the model."
        )
    response = ChatResponse.model_validate_json(reply.content)
    verdict = read_verdict(run_id, response, shown)
    async with engine.begin() as conn:
        await conn.execute(
            insert(judge_verdicts).values(
                run_id=run_id,
                question=question,
                model=model,
                from_seq=from_seq,
                to_seq=to_seq,
                status=verdict.status,
                answer=verdict.answer,
                explanation=verdict.explanation,
                citations=list(verdict.citations),
                rejection=verdict.rejection,
                request=request.model_dump(mode="json", exclude_none=True),
                response=response.model_dump(mode="json", exclude_none=True),
            )
        )
    return verdict


def read_verdict(run_id: str, response: ChatResponse, shown: frozenset[str]) -> Verdict:
    calls = [
        c for c in response.choices[0].message.tool_calls or [] if c.function.name == "verdict"
    ]
    if not calls:
        return _rejected(run_id, "the model did not call `verdict`")
    try:
        args = _VerdictArgs.model_validate_json(calls[0].function.arguments)
    except ValidationError as err:
        return _rejected(run_id, f"`verdict` arguments are invalid: {err}")
    unknown = sorted(set(args.citations) - shown)
    rejection = None
    if unknown:
        rejection = f"cites events it was not shown: {', '.join(unknown)}"
    elif args.answer == "yes" and not args.citations:
        rejection = "a `yes` cites no event"
    return Verdict(
        run_id=run_id,
        status="rejected" if rejection else "accepted",
        answer=args.answer,
        explanation=args.explanation,
        citations=tuple(args.citations),
        rejection=rejection,
    )


def _rejected(run_id: str, reason: str) -> Verdict:
    return Verdict(
        run_id=run_id,
        status="rejected",
        answer=None,
        explanation=None,
        citations=(),
        rejection=reason,
    )
