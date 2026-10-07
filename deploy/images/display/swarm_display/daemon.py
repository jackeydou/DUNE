"""The daemon: starts the screen, a window manager, the case's start scripts, and the browser,
then answers one action at a time on a unix socket only `swarmdisplay` can open.

The browser is driven through Playwright's pipe, so no debugging port is open in the sandbox. The
screen needs the cookie in `XAUTHORITY`, which only `swarmdisplay` can read.
"""

import asyncio
import json
import os
import secrets
import subprocess
import time
from typing import Any

from playwright.async_api import Error as PlaywrightError

from swarm_display import DISPLAY, SOCKET, START_DIR, STATE, XAUTHORITY, ActionError
from swarm_display.browser import Browser
from swarm_display.computer import Computer

START_SCRIPT_TIMEOUT_S = 30.0


def _environment() -> None:
    os.environ.update(
        {
            "DISPLAY": DISPLAY,
            "XAUTHORITY": str(XAUTHORITY),
            "HOME": str(STATE / "home"),
            "TMPDIR": str(STATE / "tmp"),
            "XDG_CONFIG_HOME": str(STATE / "home" / ".config"),
            "XDG_CACHE_HOME": str(STATE / "home" / ".cache"),
            "XDG_RUNTIME_DIR": str(STATE / "tmp"),
        }
    )


def _screen(width: int, height: int) -> None:
    cookie = secrets.token_hex(16)
    XAUTHORITY.touch(mode=0o600)
    subprocess.run(["xauth", "-q", "-f", str(XAUTHORITY), "add", DISPLAY, ".", cookie], check=True)
    subprocess.Popen(
        [
            "Xvfb",
            DISPLAY,
            "-screen",
            "0",
            f"{width}x{height}x24",
            "-nolisten",
            "tcp",
            "-auth",
            str(XAUTHORITY),
            "-dpi",
            "96",
        ],
        stdin=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    while subprocess.run(["xdotool", "getdisplaygeometry"], capture_output=True).returncode:
        if time.monotonic() > deadline:
            raise RuntimeError("Xvfb did not accept connections within 15s")
        time.sleep(0.1)
    subprocess.Popen(
        ["matchbox-window-manager", "-use_titlebar", "no", "-use_cursor", "yes"],
        stdin=subprocess.DEVNULL,
    )


def _start_scripts() -> None:
    if not START_DIR.is_dir():
        return
    for script in sorted(START_DIR.iterdir()):
        if script.is_file() and os.access(script, os.X_OK):
            print(f"running {script}", flush=True)
            subprocess.run(
                [str(script)], stdin=subprocess.DEVNULL, check=True, timeout=START_SCRIPT_TIMEOUT_S
            )


async def _serve(computer: Computer, browser: Browser) -> None:
    lock = asyncio.Lock()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Anything but an ActionError or a browser error is a bug: asyncio logs it to the daemon
        # log, the connection closes unanswered, and the next action is served as usual.
        try:
            line = await reader.readline()
            async with lock:
                answer = await _answer(json.loads(line), computer, browser)
            writer.write(json.dumps(answer).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle, path=str(SOCKET))
    os.chmod(SOCKET, 0o600)
    async with server:
        await server.serve_forever()


async def _answer(request: dict[str, Any], computer: Computer, browser: Browser) -> dict[str, Any]:
    tool, args = request["tool"], request["args"]
    try:
        if not isinstance(args, dict):
            raise ActionError("arguments must be a JSON object")
        if tool == "computer":
            text = await computer.act(args)
        elif tool == "browser":
            text = await browser.act(args)
        else:
            raise ActionError(f"unknown tool {tool!r}")
    except ActionError as err:
        return {"ok": False, "text": str(err)}
    except PlaywrightError as err:
        return {"ok": False, "text": f"The browser refused: {err.message}"}
    return {"ok": True, "text": text}


def run(width: int, height: int, url: str) -> int:
    _environment()
    _screen(width, height)
    _start_scripts()

    async def main() -> None:
        browser = await Browser.launch(width, height, url)
        await _serve(Computer(width, height), browser)

    asyncio.run(main())
    return 0
