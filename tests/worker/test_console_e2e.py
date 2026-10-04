"""The console in a browser against the real stack: the built `edge` with the console embedded,
the Control API, a worker, and the analysis service, with the model backend mocked. Playwright
walks M4's threshold: sign in, create a case, edit it, run it, read scores and replay, fork
(M4 spec, Plan step 6).

Needs node and pnpm (`mise run console:e2e` provides them) and a Chromium for Playwright:
its own download, or the binary PLAYWRIGHT_CHROMIUM names.
"""

import asyncio
import contextlib
import os
import secrets
import subprocess
from pathlib import Path

import pytest

from tests.containers import REPO, go_build
from tests.gateway.mock_backend import completion
from tests.worker.conftest import Platform
from tests.worker.test_edge_e2e import (
    PASSWORD,
    admin,
    analysis_address,
    edge_env,
    edge_process,
)

# Fixtures of the edge tests, used here with this module's `edge_binary`.
__all__ = ["admin", "analysis_address", "edge_env", "edge_process"]

pytestmark = pytest.mark.browser

CONSOLE = REPO / "console"


@pytest.fixture(scope="session")
def edge_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """edge with the console's current build embedded."""
    subprocess.run(["pnpm", "install", "--frozen-lockfile"], cwd=CONSOLE, check=True)
    subprocess.run(["pnpm", "build"], cwd=CONSOLE, check=True)
    return go_build("edge", tmp_path_factory.mktemp("console-edge-bin"))


async def test_the_threshold_flow_in_a_browser(
    platform: Platform,
    edge_process: str,
    admin: str,
) -> None:
    platform.backend.respond = lambda _: completion("done")
    serving = asyncio.create_task(platform.worker.serve(poll_s=0.2))
    try:
        browser = await asyncio.create_subprocess_exec(
            "pnpm",
            "exec",
            "playwright",
            "test",
            cwd=CONSOLE,
            env={
                **os.environ,
                "SWARM_URL": edge_process,
                "SWARM_USER": admin,
                "SWARM_PASSWORD": PASSWORD,
                "SWARM_WORKSPACE": f"ws-{secrets.token_hex(4)}",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(browser.communicate(), timeout=600)
    finally:
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving

    assert browser.returncode == 0, output.decode()
