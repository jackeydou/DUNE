"""`swarmeval-worker`'s command line."""

import argparse
import asyncio
import logging
import signal
import subprocess
import sys
import textwrap

import pytest

from swarmeval.worker import server
from swarmeval.worker.server import lease_seconds
from swarmeval.worker.worker import Worker, WorkerHalted


def test_a_lease_is_a_finite_number_of_seconds_above_zero() -> None:
    assert lease_seconds("30") == 30.0
    assert lease_seconds("0.5") == 0.5
    for bad in ("0", "-1", "nan", "inf", "soon"):
        with pytest.raises(argparse.ArgumentTypeError, match=f"`{bad}`"):
            lease_seconds(bad)


async def _serve(*, stay_halted: bool) -> None:
    await server.serve(
        "postgresql://nowhere",
        None,  # pyright: ignore[reportArgumentType]
        sandboxd="127.0.0.1:9",
        gateway_http="http://127.0.0.1:9",
        gateway_grpc="127.0.0.1:9",
        owner_id="w_halt",
        max_runs=1,
        allow_case_code=False,
        lease_s=30,
        mtls=None,
        stay_halted=stay_halted,
    )


@pytest.fixture
def halting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker whose first run finds the host broken."""

    async def serve(self: Worker, poll_s: float = 1.0) -> None:
        raise WorkerHalted("sandbox `a` reached sandbox `b`.")

    monkeypatch.setattr(Worker, "serve", serve)


@pytest.mark.usefixtures("halting")
async def test_a_halted_worker_exits_unless_told_to_stay() -> None:
    with pytest.raises(WorkerHalted, match="reached sandbox"):
        await _serve(stay_halted=False)


@pytest.mark.usefixtures("halting")
async def test_a_halted_worker_told_to_stay_idles_and_says_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A supervisor restarts what exits, and a restarted worker would claim the next run on
    the same broken host."""
    caplog.set_level(logging.ERROR, logger="swarmeval.worker.server")
    serving = asyncio.create_task(_serve(stay_halted=True))
    await asyncio.sleep(0.2)
    assert not serving.done()
    assert "worker w_halt halted: sandbox `a` reached sandbox `b`." in caplog.text
    assert "Fix the host, then restart the worker." in caplog.text
    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving


@pytest.mark.parametrize("stop", [signal.SIGTERM, signal.SIGINT])
def test_a_stop_signal_ends_a_service_through_its_cleanup_and_exits_zero(
    stop: signal.Signals,
) -> None:
    """`docker stop` sends SIGTERM; left to Python's default, a container's first process
    ignores it and is killed without cleanup. Ctrl-C ends the same way, with no traceback."""
    program = textwrap.dedent(
        """
        import asyncio
        from swarmeval.config import run_service

        async def main():
            try:
                print("serving", flush=True)
                await asyncio.Event().wait()
            finally:
                print("cleaned up", flush=True)

        run_service(main())
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    assert proc.stdout.readline() == "serving\n"
    proc.send_signal(stop)
    assert proc.stdout.read() == "cleaned up\n"
    assert proc.wait(timeout=10) == 0
    assert proc.stderr.read() == ""
