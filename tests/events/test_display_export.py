"""Screenshots through the record: tool events and contexts hold references, the export embeds
the images once, and the transcript check rebuilds requests with them."""

import base64
import hashlib
import struct
from pathlib import Path
from typing import Any, cast

import pytest
from inspect_ai.event import ModelEvent, ToolEvent
from inspect_ai.log import read_eval_log
from inspect_ai.model import ChatMessageTool, ContentImage, ContentText

from swarmeval.events import StoredRun, assemble, write_eval
from swarmeval.events.export import image_hashes
from swarmeval.runtime.display import DISPLAY_TOOLS, SCREENSHOT
from swarmeval.runtime.messages import ImageRef, ToolMessage
from swarmeval.runtime.records import CollectedFile, Exec, ExecResult
from swarmeval.runtime.specs import initial_context
from swarmeval.runtime.tools import SHELL
from swarmeval.sandbox.client import png_info
from swarmeval.worker.transcript import check_transcript
from tests.events.test_export import HEADER, stored
from tests.runtime.fakes import ANY_IMAGE, FakeSandbox, Harness, agent, call, harness, reply
from tests.worker.test_transcript import transcript_of, with_context

SHOTS = 4


def png(n: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 1024, n)


IMAGES = {hashlib.sha256(png(n)).hexdigest(): png(n) for n in range(1, SHOTS + 1)}


async def screenshots() -> Harness:
    taken = iter(IMAGES.items())

    def handler(command: Exec) -> ExecResult:
        sha, data = next(taken)
        collected = CollectedFile(path=SCREENSHOT, size=len(data), sha256=sha, png=png_info(data))
        return ExecResult(exit_code=0, stdout="Did it.\n", stderr="", collected=(collected,))

    look = call("computer", '{"action": "screenshot"}')
    h = harness(
        (agent("a", tools=("computer",)),),
        {"a": [*(reply("", look) for _ in range(SHOTS)), reply("done")]},
        sandbox=FakeSandbox(handler=handler),
        tools=(SHELL, *DISPLAY_TOOLS),
    )
    await h.loop.run()
    return h


def with_images(run: StoredRun) -> StoredRun:
    return StoredRun(
        workspace=run.workspace, events=run.events, messages=run.messages, images=IMAGES
    )


async def test_the_export_embeds_each_image_once_and_shows_what_the_model_saw(
    tmp_path: Path,
) -> None:
    h = await screenshots()
    run = with_images(stored(h.store))
    assert image_hashes(run) == set(IMAGES)

    path = tmp_path / "sample.eval"
    write_eval(assemble(HEADER, run), path)
    (sample,) = read_eval_log(path, resolve_attachments=True).samples or []

    tools = [e for e in sample.events if isinstance(e, ToolEvent)]
    first = tools[0].result
    assert isinstance(first, list)
    assert first[0] == ContentText(text="Did it.\n")
    image = first[1]
    assert isinstance(image, ContentImage)
    assert image.image == f"data:image/png;base64,{base64.b64encode(png(1)).decode()}"
    last = [e for e in sample.events if isinstance(e, ModelEvent)][-1]
    shown = [m for m in last.input if isinstance(m, ChatMessageTool)]
    assert len(shown) == SHOTS
    parts = [m.content[1] for m in shown if isinstance(m.content, list)]
    assert parts[0] == ContentText(text="[image omitted]"), "only the last 3 images are sent"
    assert all(isinstance(p, ContentImage) for p in parts[1:])


async def test_an_export_without_the_images_is_refused() -> None:
    h = await screenshots()

    with pytest.raises(ValueError, match="names 4 image"):
        assemble(HEADER, stored(h.store))


async def test_tool_events_carry_image_references_under_schema_10() -> None:
    h = await screenshots()
    run = stored(h.store)

    payloads = [cast(dict[str, Any], r.payload) for r in run.events]
    tool = next(p for p in payloads if p["event"] == "tool")
    ours = tool["metadata"]["swarmeval"]
    assert ours["schema_version"] == 10
    sha = next(iter(IMAGES))
    assert ours["images"] == [
        {"sha256": sha, "media_type": "image/png", "width": 1024, "height": 1}
    ]
    assert ours["exec"]["collected"][0]["sha256"] == sha
    assert tool["result"] == "Did it.\n"


async def test_the_transcript_check_rebuilds_requests_with_their_images() -> None:
    h = await screenshots()
    transcript = transcript_of(h.store)

    def check(images: dict[str, bytes] | None = None) -> bool:
        return check_transcript(
            transcript,
            prompts={"a": initial_context(agent("a", tools=("computer",)))},
            tools=h.loop.tool_schemas(),
            images=images or ANY_IMAGE,
        ).consistent

    assert check()
    message = transcript.contexts[("a", 0)][3]
    assert isinstance(message, ToolMessage)
    swapped = message.model_copy(update={"images": (ImageRef(sha256="f" * 64, width=1, height=1),)})
    tampered = check_transcript(
        with_context(transcript, "a", 3, swapped),
        prompts={"a": initial_context(agent("a", tools=("computer",)))},
        tools=h.loop.tool_schemas(),
        images=ANY_IMAGE,
    )
    assert not tampered.consistent
