"""The display stack of a sandbox: a virtual screen, a browser on it, and the daemon that drives
both for the `computer` and `browser` tools (deploy/images/display/README.md).

Everything here runs inside the sandbox as the user `swarmdisplay`.
"""

from pathlib import Path

STATE = Path("/run/swarm-display")
SOCKET = STATE / "daemon.sock"
LOG = STATE / "daemon.log"
SCREENSHOT = STATE / "out" / "screenshot.png"
"""Written by an action that has an image; sandboxd collects and removes it after the call."""
DISPLAY = ":1"
XAUTHORITY = STATE / "Xauthority"
START_DIR = Path("/etc/swarm-display/start.d")


class ActionError(Exception):
    """The action could not be done; the message is what the agent sees."""
