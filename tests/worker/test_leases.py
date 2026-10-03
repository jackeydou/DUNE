"""A worker's lease renewal (`swarmeval.worker.leases`), with the queue replaced."""

import asyncio
from collections.abc import Mapping

import pytest
from sqlalchemy.exc import OperationalError

from swarmeval.worker.leases import LeaseLost, Leases


class FakeQueue:
    """Renews whatever `keep` says; raises once `down` is set."""

    def __init__(self) -> None:
        self.keep: set[str] | None = None
        self.down = False
        self.calls: list[dict[str, int]] = []

    async def renew(self, owner_id: str, held: Mapping[str, int], lease_s: float) -> set[str]:
        self.calls.append(dict(held))
        if self.down:
            raise OperationalError("UPDATE control.runs", {}, ConnectionRefusedError())
        return set(held) if self.keep is None else set(held) & self.keep


async def _forever(started: asyncio.Event, stopped: list[str]) -> None:
    started.set()
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        stopped.append("cancelled")
        raise


async def test_work_that_ends_returns_its_value_and_is_no_longer_held() -> None:
    queue = FakeQueue()
    leases = Leases(queue, "w", lease_s=0.15)

    async def work() -> str:
        await asyncio.sleep(0.2)
        return "done"

    async with leases.renewing():
        assert await leases.hold("r1", 3, work()) == "done"
        await asyncio.sleep(0.1)

    assert {"r1": 3} in queue.calls
    assert queue.calls[-1] == {}


async def test_a_run_the_renewal_leaves_out_is_stopped() -> None:
    queue = FakeQueue()
    leases = Leases(queue, "w_lost", lease_s=0.15)
    started, stopped = asyncio.Event(), list[str]()

    async with leases.renewing():
        holding = asyncio.create_task(leases.hold("r1", 2, _forever(started, stopped)))
        await started.wait()
        queue.keep = set()
        with pytest.raises(LeaseLost, match="`w_lost` lost the lease of run `r1` at owner_epoch 2"):
            await asyncio.wait_for(holding, 2)

    assert stopped == ["cancelled"]


async def test_failing_renewals_stop_every_run_before_the_lease_runs_out() -> None:
    queue = FakeQueue()
    lease_s = 0.6
    leases = Leases(queue, "w_cut_off", lease_s=lease_s)
    clock = asyncio.get_running_loop()
    started, stopped = asyncio.Event(), list[str]()

    async with leases.renewing():
        holding = asyncio.create_task(leases.hold("r1", 1, _forever(started, stopped)))
        await started.wait()
        await asyncio.sleep(lease_s / 3 + 0.05)
        assert queue.calls[-1] == {"r1": 1}
        last_renewed = clock.time()
        queue.down = True
        with pytest.raises(LeaseLost, match="renewals failed"):
            await asyncio.wait_for(holding, 2)
        stopped_after = clock.time() - last_renewed

    assert len(queue.calls) >= 3, "one failed renewal alone does not stop the run"
    assert stopped_after < lease_s
    assert stopped == ["cancelled"]


async def test_a_cancel_from_outside_is_a_cancel_not_a_lost_lease() -> None:
    leases = Leases(FakeQueue(), "w", lease_s=10)
    started, stopped = asyncio.Event(), list[str]()

    async with leases.renewing():
        holding = asyncio.create_task(leases.hold("r1", 1, _forever(started, stopped)))
        await started.wait()
        holding.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holding

    assert stopped == ["cancelled"]


async def test_holding_a_run_again_at_a_newer_epoch_stops_the_older_hold() -> None:
    leases = Leases(FakeQueue(), "w_stalled", lease_s=10)
    started, stopped = asyncio.Event(), list[str]()

    async def cleanup() -> str:
        return "removed"

    async with leases.renewing():
        older = asyncio.create_task(leases.hold("r1", 1, _forever(started, stopped)))
        await started.wait()
        assert await leases.hold("r1", 2, cleanup()) == "removed"
        with pytest.raises(LeaseLost, match="taken over again at owner_epoch 2"):
            await older

    assert stopped == ["cancelled"]
