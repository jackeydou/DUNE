"""One event as one line of text, for what people and the judge read: the judge's transcript and
the timeline.

Line breaks inside an event are escaped, so run content cannot start a line that looks like
another event.
"""

import json

from pydantic import JsonValue

from swarmeval.detect.search import one_line


def event_line(payload: dict[str, JsonValue], limit: int) -> str:
    """The event's description on one line, its first `limit` characters and a mark saying how
    long it was when it is longer."""
    text = describe(payload)
    if len(text) <= limit:
        return one_line(text)
    return one_line(text[:limit]) + f" …[cut: {len(text)} characters]"


def describe(payload: dict[str, JsonValue]) -> str:
    match payload.get("event"):
        case "model":
            return "model: " + _model_output(payload)
        case "tool":
            args = json.dumps(payload.get("arguments"), ensure_ascii=False)
            return f"tool {payload.get('function')}({args}) -> {payload.get('result')}"
        case "sandbox" if (probe := _probe(payload)) is not None:
            findings = json.dumps(probe.get("findings", []), ensure_ascii=False)
            return f"isolation self-check, {probe.get('step')} step: findings {findings}"
        case "sandbox":
            return f"sandbox exec by an extension: {payload.get('cmd')} -> {payload.get('output')}"
        case "score":
            score = payload.get("score")
            value, why = (
                (score.get("value"), score.get("explanation"))
                if isinstance(score, dict)
                else (None, None)
            )
            return f"score {payload.get('scorer')} = {value}: {why}"
        case "info":
            data = json.dumps(payload.get("data"), ensure_ascii=False)
            return f"{payload.get('source')}: {data}"
        case kind:
            return f"{kind}: {json.dumps(payload, ensure_ascii=False)}"


def _probe(payload: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
    """An `isolation_probe` event's `probe`, without its scripts, which are the platform's and
    long: a reader needs only what the probes found."""
    metadata = payload.get("metadata")
    swarmeval = metadata.get("swarmeval") if isinstance(metadata, dict) else None
    probe = swarmeval.get("probe") if isinstance(swarmeval, dict) else None
    return probe if isinstance(probe, dict) else None


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
