import hashlib
import json
import struct

import pytest
from pydantic import ValidationError

from swarmeval.runtime.display import (
    BROWSER,
    COMPUTER,
    DISPLAY_TOOLS,
    SCREENSHOT,
    BrowserArgs,
    ComputerArgs,
    display_output,
)
from swarmeval.runtime.messages import ImageRef, ToolMessage, UserMessage, visible_images
from swarmeval.runtime.records import CollectedFile, Exec, ExecResult, PngInfo, ToolCallRecord
from swarmeval.runtime.tools import BUILTIN_TOOL_NAMES, SHELL
from swarmeval.sandbox.client import png_info
from tests.runtime.fakes import FakeSandbox, agent, call, harness, reply

SHOT = "ab" * 32


def png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
    )


def shot(collected: CollectedFile, exit_code: int = 0) -> ExecResult:
    return ExecResult(
        exit_code=exit_code, stdout="Did `click`.\n", stderr="", collected=(collected,)
    )


def test_display_tools_are_builtin_names() -> None:
    assert {t.name for t in DISPLAY_TOOLS} <= set(BUILTIN_TOOL_NAMES)


@pytest.mark.parametrize(
    ("args", "missing"),
    [
        ({"action": "click"}, "`coordinate`"),
        ({"action": "drag", "coordinate": [1, 2]}, "`start_coordinate`"),
        ({"action": "type"}, "`text`"),
        ({"action": "scroll"}, "`direction`"),
    ],
)
def test_computer_actions_require_their_arguments(args: dict[str, object], missing: str) -> None:
    with pytest.raises(ValidationError, match=missing):
        ComputerArgs.model_validate(args)


def test_computer_points_are_two_integers() -> None:
    with pytest.raises(ValidationError):
        ComputerArgs.model_validate({"action": "click", "coordinate": [1, 2, 3]})
    with pytest.raises(ValidationError):
        ComputerArgs.model_validate({"action": "click", "coordinate": [1.5, 2]})


@pytest.mark.parametrize(
    ("args", "missing"),
    [
        ({"action": "navigate"}, "`url`"),
        ({"action": "type", "ref": "e3"}, "`text`"),
        ({"action": "select", "ref": "e3"}, "`values`"),
        ({"action": "tab_select"}, "`index`"),
    ],
)
def test_browser_actions_require_their_arguments(args: dict[str, object], missing: str) -> None:
    with pytest.raises(ValidationError, match=missing):
        BrowserArgs.model_validate(args)


def test_browser_refs_are_plain_tokens() -> None:
    with pytest.raises(ValidationError):
        BrowserArgs.model_validate({"action": "click", "ref": "e1] >> css=body"})


def test_display_tools_run_as_the_display_user_and_collect_the_screenshot() -> None:
    command = COMPUTER.build(ComputerArgs.model_validate({"action": "click", "coordinate": [3, 4]}))

    assert command.user == "swarmdisplay"
    assert command.collect == (SCREENSHOT,)
    assert command.argv[:2] == ("swarm-display", "computer")
    assert json.loads(command.argv[2]) == {
        "action": "click",
        "coordinate": [3, 4],
        "button": "left",
        "count": 1,
        "amount": 3,
        "seconds": 1.0,
    }
    assert BROWSER.build(BrowserArgs(action="back")).argv[:2] == ("swarm-display", "browser")


def test_a_collected_png_becomes_an_image() -> None:
    result = display_output(
        call("computer", id="c1"),
        shot(
            CollectedFile(
                path=SCREENSHOT, size=100, sha256=SHOT, png=PngInfo(width=1024, height=768)
            )
        ),
    )

    assert not result.is_error
    assert result.content == "Did `click`.\n"
    assert result.images == (ImageRef(sha256=SHOT, width=1024, height=768),)


def test_no_screenshot_means_no_image() -> None:
    result = display_output(
        call("browser", id="c1"), shot(CollectedFile(path=SCREENSHOT, missing=True))
    )

    assert (result.is_error, result.images) == (False, ())


@pytest.mark.parametrize(
    ("collected", "says"),
    [
        (CollectedFile(path=SCREENSHOT, size=9, sha256=SHOT), "not a PNG"),
        (CollectedFile(path=SCREENSHOT, size=99_000_000), "over sandboxd's limit"),
    ],
)
def test_an_unusable_screenshot_is_an_error_the_agent_sees(
    collected: CollectedFile, says: str
) -> None:
    result = display_output(call("computer", id="c1"), shot(collected))

    assert result.is_error
    assert says in result.content
    assert result.images == ()


def test_png_info_reads_the_header_and_refuses_the_rest() -> None:
    assert png_info(png(1024, 768)) == PngInfo(width=1024, height=768)
    assert png_info(b"GIF89a" + bytes(30)) is None
    assert png_info(png(0, 768)) is None
    assert png_info(png(100_000, 10)) is None
    assert png_info(png(10, 10)[:20]) is None


def test_visible_images_are_the_latest() -> None:
    a, b, c = (ImageRef(sha256=x * 64, width=1, height=1) for x in "abc")
    messages = (
        UserMessage(content="go"),
        ToolMessage(tool_call_id="1", content="", images=(a, b)),
        ToolMessage(tool_call_id="2", content=""),
        ToolMessage(tool_call_id="3", content="", images=(c,)),
    )

    assert visible_images(messages, 2) == {(1, 1), (3, 0)}
    assert visible_images(messages, 5) == {(1, 0), (1, 1), (3, 0)}
    assert visible_images(messages, 0) == set()


async def test_a_screenshot_reaches_the_agents_context_and_the_record() -> None:
    image = png(1024, 768)
    sha = hashlib.sha256(image).hexdigest()

    def handler(command: Exec) -> ExecResult:
        assert command.user == "swarmdisplay"
        return shot(
            CollectedFile(path=SCREENSHOT, size=len(image), sha256=sha, png=png_info(image))
        )

    h = harness(
        (agent(tools=("computer",)),),
        {"a": [reply("", call("computer", '{"action": "screenshot"}')), reply("done")]},
        sandbox=FakeSandbox(handler=handler),
        tools=(SHELL, *DISPLAY_TOOLS),
    )

    await h.loop.run()

    message = h.store.messages("a")[3]
    assert isinstance(message, ToolMessage)
    assert message.images == (ImageRef(sha256=sha, width=1024, height=768),)
    record = h.store.records("tool")[0].record
    assert isinstance(record, ToolCallRecord)
    assert record.result.images == message.images
    _, second = h.model.requests[1]
    assert second.options.max_images == 3
