"""`computer` and `browser`: tools that drive a sandbox's display (docs/agent-runtime.md#tools).

Both run `swarm-display` inside the sandbox as the display's own user, which only these tools
run as, so an agent's shell cannot reach the screen. An image an action produces is collected by
sandboxd from outside the key paths and reaches the agent as an `ImageRef`.
"""

import json
from collections.abc import Callable
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from swarmeval.runtime.messages import ImageRef, ToolCall
from swarmeval.runtime.records import Exec, ExecResult, ToolResult
from swarmeval.runtime.tools import SandboxTool, exec_output

DISPLAY_USER = "swarmdisplay"
"""Runs the display stack and these tools. Display images have it (deploy/images/display)."""
DISPLAY_STATE = "/run/swarm-display"
"""The display's state directory, kept out of every key path."""
DISPLAY_TOOL_NAMES = ("computer", "browser")
SCREENSHOT = f"{DISPLAY_STATE}/out/screenshot.png"
ACTION_TIMEOUT_S = 90.0

Point = Annotated[
    list[int], Field(min_length=2, max_length=2, description="[x, y] in screen pixels.")
]


def _require(args: BaseModel, action: str, needs: dict[str, tuple[str, ...]]) -> None:
    missing = [f for f in needs.get(action, ()) if getattr(args, f) is None]
    if missing:
        names = ", ".join(f"`{f}`" for f in missing)
        raise ValueError(f"action `{action}` needs {names}")


class ComputerArgs(BaseModel):
    action: Literal[
        "screenshot",
        "click",
        "mouse_move",
        "drag",
        "type",
        "key",
        "scroll",
        "wait",
        "cursor_position",
    ] = Field(description="What to do. Every action but `cursor_position` returns a screenshot.")
    coordinate: Point | None = Field(
        default=None,
        description="Where to click or move to (`click`, `mouse_move`, `drag`'s end), or where "
        "to scroll (`scroll`, optional).",
    )
    start_coordinate: Point | None = Field(default=None, description="Where `drag` starts.")
    button: Literal["left", "middle", "right"] = Field(default="left", description="For `click`.")
    count: int = Field(default=1, ge=1, le=3, description="Clicks: 2 for a double click.")
    text: str | None = Field(
        default=None,
        max_length=10_000,
        description="For `type`, the text to type. For `key`, xdotool key names, combos joined "
        "with `+` and several keys separated by spaces: `ctrl+l`, `Return`, `alt+Tab`.",
    )
    direction: Literal["up", "down", "left", "right"] | None = Field(
        default=None, description="For `scroll`."
    )
    amount: int = Field(default=3, ge=1, le=20, description="Wheel clicks for `scroll`.")
    seconds: float = Field(default=1.0, gt=0, le=10, description="For `wait`.")

    @model_validator(mode="after")
    def _needs(self) -> Self:
        _require(
            self,
            self.action,
            {
                "click": ("coordinate",),
                "mouse_move": ("coordinate",),
                "drag": ("start_coordinate", "coordinate"),
                "type": ("text",),
                "key": ("text",),
                "scroll": ("direction",),
            },
        )
        return self


class BrowserArgs(BaseModel):
    action: Literal[
        "navigate",
        "back",
        "forward",
        "reload",
        "snapshot",
        "screenshot",
        "click",
        "hover",
        "type",
        "select",
        "press",
        "scroll",
        "wait",
        "tabs",
        "tab_new",
        "tab_select",
        "tab_close",
    ] = Field(description="What to do. Every action returns the page snapshot afterwards.")
    url: str | None = Field(
        default=None, max_length=8192, description="For `navigate`, and optionally `tab_new`."
    )
    ref: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9]{1,32}$",
        description="The element to act on, as its `ref` in the latest snapshot, such as `e12` "
        "(`click`, `hover`, `type`, `select`).",
    )
    text: str | None = Field(
        default=None, max_length=10_000, description="For `type`: replaces the field's text."
    )
    submit: bool = Field(default=False, description="For `type`: press Enter afterwards.")
    values: list[str] | None = Field(
        default=None, description="For `select`: option values or labels."
    )
    key: str | None = Field(
        default=None,
        max_length=100,
        description="For `press`: a key or combo such as `Enter`, `Escape`, `Control+a`.",
    )
    direction: Literal["up", "down", "left", "right"] = Field(
        default="down", description="For `scroll`."
    )
    amount: int = Field(default=3, ge=1, le=20, description="For `scroll`: screens' worth / 2.")
    seconds: float = Field(default=1.0, gt=0, le=10, description="For `wait`.")
    index: int | None = Field(
        default=None, ge=1, description="Tab number, from 1 (`tab_select`, `tab_close`)."
    )
    screenshot: bool = Field(
        default=False,
        description="Attach a screenshot of the page after the action. `screenshot` always does.",
    )

    @model_validator(mode="after")
    def _needs(self) -> Self:
        _require(
            self,
            self.action,
            {
                "navigate": ("url",),
                "click": ("ref",),
                "hover": ("ref",),
                "type": ("ref", "text"),
                "select": ("ref", "values"),
                "press": ("key",),
                "tab_select": ("index",),
            },
        )
        return self


def _build(tool: str) -> Callable[[BaseModel], Exec]:
    def build(args: BaseModel) -> Exec:
        arguments = json.dumps(args.model_dump(exclude_none=True))
        return Exec(
            argv=("swarm-display", tool, arguments),
            timeout_s=ACTION_TIMEOUT_S,
            user=DISPLAY_USER,
            collect=(SCREENSHOT,),
        )

    return build


def display_output(call: ToolCall, result: ExecResult) -> ToolResult:
    """The action's text, with its screenshot as an image when it took one."""
    text = exec_output(call, result)
    images: list[ImageRef] = []
    problems: list[str] = []
    for c in result.collected:
        if c.missing:
            continue
        if c.sha256 is None:
            problems.append(f"[the screenshot was {c.size} bytes, over sandboxd's limit]")
        elif c.png is None:
            problems.append("[the display returned a screenshot that is not a PNG image]")
        else:
            images.append(ImageRef(sha256=c.sha256, width=c.png.width, height=c.png.height))
    content = text.content + "".join(f"\n{p}" for p in problems)
    return text.model_copy(
        update={
            "content": content,
            "is_error": text.is_error or bool(problems),
            "images": tuple(images),
        }
    )


COMPUTER = SandboxTool(
    name="computer",
    description=(
        "Use the mouse and keyboard on your sandbox's screen, a desktop showing a web browser. "
        "Coordinates are screen pixels from the top left, as in the screenshots. Each action "
        "returns a screenshot taken after it."
    ),
    args=ComputerArgs,
    build=_build("computer"),
    output=display_output,
)

BROWSER = SandboxTool(
    name="browser",
    description=(
        "Drive the web browser on your sandbox's screen. Every action returns the active tab's "
        "URL, title, and an accessibility snapshot in which each element has a `[ref=eN]`; act "
        "on elements by that ref. Refs change when the page does: use the latest snapshot. The "
        "sandbox has no internet; pages are served from inside it."
    ),
    args=BrowserArgs,
    build=_build("browser"),
    output=display_output,
)

DISPLAY_TOOLS = (COMPUTER, BROWSER)
"""Handed to `RunLoop` beside `BUILTIN_TOOLS`."""
