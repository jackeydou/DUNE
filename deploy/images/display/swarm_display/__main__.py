"""`swarm-display start | computer <json> | browser <json> | daemon`.

`start` launches the daemon, waits until the screen and the browser are up, prints the daemon's
pid, and exits; sandboxd runs it once, when it creates the sandbox. `computer` and `browser`
send one action to the daemon and print its text; an image, if any, is at `SCREENSHOT`. Exit
status 1 means the action failed, and the reason is on stderr.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time

from swarm_display import LOG, SCREENSHOT, SOCKET, STATE

START_TIMEOUT_S = 50.0
ACTION_TIMEOUT_S = 180.0


def _start(width: int, height: int, url: str) -> int:
    if SOCKET.exists():
        print(f"the display is already running ({SOCKET} exists)", file=sys.stderr)
        return 1
    for sub in ("home", "tmp", "out", "profile"):
        (STATE / sub).mkdir(mode=0o700, exist_ok=True)
    argv = [sys.executable, "-s", "-m", "swarm_display", "daemon"]
    argv += ["--width", str(width), "--height", str(height), "--url", url]
    with open(LOG, "ab") as log:
        daemon = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            cwd=STATE,
        )
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if daemon.poll() is not None:
            print(f"the display daemon exited {daemon.returncode}:\n{_log_tail()}", file=sys.stderr)
            return 1
        if SOCKET.exists():
            print(daemon.pid)
            return 0
        time.sleep(0.1)
    daemon.kill()
    print(f"the display was not up after {START_TIMEOUT_S:.0f}s:\n{_log_tail()}", file=sys.stderr)
    return 1


def _log_tail() -> str:
    try:
        return LOG.read_text(errors="replace")[-4000:]
    except FileNotFoundError:
        return "(no log)"


def _act(tool: str, raw: str) -> int:
    try:
        args = json.loads(raw)
    except ValueError as err:
        print(f"arguments are not JSON: {err}", file=sys.stderr)
        return 1
    try:
        SCREENSHOT.unlink(missing_ok=True)
    except PermissionError:
        print(f"swarm-display {tool} must run as the display's user, swarmdisplay", file=sys.stderr)
        return 1
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(ACTION_TIMEOUT_S)
        try:
            conn.connect(str(SOCKET))
        except OSError as err:
            print(f"the display is not running ({SOCKET}: {err})", file=sys.stderr)
            return 1
        conn.sendall(json.dumps({"tool": tool, "args": args}).encode() + b"\n")
        with conn.makefile("rb") as reply:
            line = reply.readline()
    if not line:
        print("the display daemon closed the connection without an answer", file=sys.stderr)
        return 1
    answer = json.loads(line)
    if not answer["ok"]:
        print(answer["text"], file=sys.stderr)
        return 1
    sys.stdout.write(answer["text"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="swarm-display")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "daemon"):
        p = sub.add_parser(name)
        p.add_argument("--width", type=int, required=True)
        p.add_argument("--height", type=int, required=True)
        p.add_argument("--url", default="about:blank")
    for name in ("computer", "browser"):
        sub.add_parser(name).add_argument("arguments")
    opts = parser.parse_args()
    os.umask(0o077)
    match opts.command:
        case "start":
            return _start(opts.width, opts.height, opts.url)
        case "daemon":
            from swarm_display.daemon import run

            return run(opts.width, opts.height, opts.url)
        case tool:
            return _act(tool, opts.arguments)


if __name__ == "__main__":
    sys.exit(main())
