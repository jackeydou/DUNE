"""`computer`: the mouse, the keyboard, and screenshots of the whole screen, in screen pixels.

The worker validates arguments before they get here (swarmeval/runtime/display.py); this module
checks only what it needs to act safely, such as coordinates inside the screen.
"""

import asyncio
from typing import Any

from PIL import ImageGrab

from swarm_display import DISPLAY, SCREENSHOT, ActionError

SETTLE_S = 0.4
"""Time for the screen to redraw after an action, before its screenshot."""
TYPE_DELAY_MS = 12
BUTTONS = {"left": "1", "middle": "2", "right": "3"}
WHEEL = {"up": "4", "down": "5", "left": "6", "right": "7"}


class Computer:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height

    async def act(self, args: dict[str, Any]) -> str:
        action = args.get("action")
        match action:
            case "screenshot":
                pass
            case "click":
                x, y = self._point(args, "coordinate")
                button = BUTTONS[args.get("button", "left")]
                count = str(args.get("count", 1))
                await _xdotool("mousemove", "--sync", x, y, "click", "--repeat", count, button)
            case "mouse_move":
                await _xdotool("mousemove", "--sync", *self._point(args, "coordinate"))
            case "drag":
                x0, y0 = self._point(args, "start_coordinate")
                x1, y1 = self._point(args, "coordinate")
                await _xdotool("mousemove", "--sync", x0, y0, "mousedown", "1")
                await _xdotool("mousemove", "--sync", x1, y1, "mouseup", "1")
            case "type":
                await _xdotool("type", "--delay", str(TYPE_DELAY_MS), "--", str(args["text"]))
            case "key":
                await _xdotool("key", "--", *str(args["text"]).split())
            case "scroll":
                if "coordinate" in args:
                    await _xdotool("mousemove", "--sync", *self._point(args, "coordinate"))
                wheel = WHEEL[args["direction"]]
                await _xdotool("click", "--repeat", str(args.get("amount", 3)), wheel)
            case "wait":
                await asyncio.sleep(float(args.get("seconds", 1)))
            case "cursor_position":
                out = await _xdotool("getmouselocation", "--shell")
                pos = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
                return f"The mouse is at ({pos['X']}, {pos['Y']}) on a {self._size()} screen.\n"
            case _:
                raise ActionError(f"unknown computer action {action!r}")
        if action != "screenshot":
            await asyncio.sleep(SETTLE_S)
        await asyncio.to_thread(self._screenshot)
        return f"Did `{action}`. Screenshot of the {self._size()} screen attached.\n"

    def _size(self) -> str:
        return f"{self.width}x{self.height}"

    def _point(self, args: dict[str, Any], name: str) -> tuple[str, str]:
        point = args.get(name)
        if not isinstance(point, list) or len(point) != 2:
            raise ActionError(f"`{name}` must be [x, y]")
        x, y = point
        if not (isinstance(x, int) and isinstance(y, int)):
            raise ActionError(f"`{name}` must be two integers, got {point!r}")
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ActionError(
                f"`{name}` {point!r} is off the {self._size()} screen: x runs 0 to "
                f"{self.width - 1}, y 0 to {self.height - 1}"
            )
        return str(x), str(y)

    def _screenshot(self) -> None:
        image = ImageGrab.grab(xdisplay=DISPLAY)
        partial = SCREENSHOT.with_suffix(".partial")
        image.save(partial, format="PNG")
        partial.replace(SCREENSHOT)


async def _xdotool(*argv: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "xdotool",
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode:
        raise ActionError(f"xdotool {' '.join(argv)} failed: {err.decode(errors='replace')}")
    return out.decode(errors="replace")
