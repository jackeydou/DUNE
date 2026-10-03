"""The `async` turn policy: every agent steps in its own task, as fast as its model answers
(M2 spec decision 10, docs/agent-runtime.md#turn-policies).

An agent whose last response called no tool waits until a message is due for it. The run ends
when every agent is waiting, or has used up its own `max_turns`, and no message is due or held
by time; or when a run-wide limit, a stop, or a failure ends it for all. The order of events is
recorded, not reproducible: two agents' steps interleave as their calls return.
"""

import asyncio
import contextlib
from collections.abc import Sequence
from typing import Literal, Protocol

from swarmeval.gateway.bus import MessageBus
from swarmeval.runtime.extensions.dispatch import HookDispatcher
from swarmeval.runtime.specs import RunOutcome
from swarmeval.runtime.state import AgentRun, Stopped


class Turns(Protocol):
    """What the driver needs of the loop."""

    @property
    def agents(self) -> Sequence[AgentRun]: ...
    @property
    def bus(self) -> MessageBus: ...
    @property
    def max_turns(self) -> int | None:
        """Each agent's own."""
        ...

    async def hook_point(self, d: HookDispatcher) -> None:
        """Raises `Stopped` once a stop is requested."""
        ...

    def stop_requested(self, d: HookDispatcher) -> bool: ...
    def time_left(self) -> float | None:
        """Seconds of `wall_clock` left, paused time left out; `None` without the limit."""
        ...

    async def run_limit(self) -> tuple[RunOutcome, str] | None:
        """A run-wide limit reached, recorded, with its `limit` event; `None` when none is."""
        ...

    async def agent_limit(self, agent: AgentRun) -> None:
        """Records that `agent` used up its own turns."""
        ...

    async def step(self, d: HookDispatcher, agent: AgentRun) -> None: ...
    def outcome(
        self, status: Literal["finished", "stopped", "limit"], reason: str | None
    ) -> RunOutcome: ...


class AsyncDriver:
    def __init__(self, turns: Turns, d: HookDispatcher) -> None:
        self._t = turns
        self._d = d
        self._wake = asyncio.Condition()
        self._waiting: set[str] = set()
        self._capped: set[str] = set()
        self._end: RunOutcome | None = None
        self.end_cause: str | None = None
        """The event that ended the run, when one did: a limit, or a stop's intervention."""

    async def run(self) -> RunOutcome:
        """Raises the first failure of any agent's task; the other tasks are cancelled."""
        try:
            async with asyncio.TaskGroup() as group:
                for agent in self._t.agents:
                    group.create_task(self._agent(agent))
        except BaseExceptionGroup as failures:
            raise failures.exceptions[0] from failures
        assert self._end is not None, "every task ends by setting the outcome"
        return self._end

    async def poke(self) -> None:
        """Wakes waiting agents to look again: mail may be due, or the run may be over."""
        async with self._wake:
            self._wake.notify_all()

    async def _finish(self, outcome: RunOutcome, cause: str | None = None) -> None:
        if self._end is None:
            self._end, self.end_cause = outcome, cause
        await self.poke()

    async def _agent(self, agent: AgentRun) -> None:
        max_turns = self._t.max_turns
        try:
            while self._end is None:
                await self._t.hook_point(self._d)
                limit = await self._t.run_limit()
                if limit is not None:
                    await self._finish(*limit)
                    return
                if agent.finished:
                    match await self._mail(agent):
                        case "end":
                            return
                        case "look":
                            continue
                        case "mail":
                            agent.finished = False
                if max_turns is not None and agent.turn >= max_turns:
                    await self._t.agent_limit(agent)
                    async with self._wake:
                        self._capped.add(agent.spec.id)
                        self._end_if_quiet()
                        self._wake.notify_all()
                    return
                await self._t.step(self._d, agent)
                await self.poke()
        except Stopped as stop:
            await self._finish(self._t.outcome("stopped", stop.reason), stop.cause)

    async def _mail(self, agent: AgentRun) -> Literal["mail", "look", "end"]:
        """Waits until a message is due for `agent` (`mail`); until a stop is asked for or the
        wall clock runs out, which the next hook point or limit check acts on (`look`); or until
        the run is over (`end`)."""
        agent_id = agent.spec.id
        async with self._wake:
            self._waiting.add(agent_id)
            try:
                while True:
                    if self._end is not None:
                        return "end"
                    if self._t.stop_requested(self._d):
                        return "look"
                    if self._t.bus.has_mail(agent_id, agent.turn + 1):
                        return "mail"
                    if self._end_if_quiet():
                        self._wake.notify_all()
                        return "end"
                    left = self._t.time_left()
                    if left is not None and left <= 0:
                        return "look"
                    timeouts = [
                        t for t in (self._t.bus.next_due_in(agent_id), left) if t is not None
                    ]
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._wake.wait(), min(timeouts) if timeouts else None
                        )
            finally:
                self._waiting.discard(agent_id)

    def _end_if_quiet(self) -> bool:
        """Ends the run when it is quiet; called with `_wake` held."""
        if self._end is not None or not self._quiet():
            return self._end is not None
        reason = f"max_turns reached by {', '.join(sorted(self._capped))}" if self._capped else None
        self._end = self._t.outcome("limit" if self._capped else "finished", reason)
        return True

    def _quiet(self) -> bool:
        """No agent is stepping, and none has mail that is due or will come due by time."""
        bus = self._t.bus
        for a in self._t.agents:
            agent_id = a.spec.id
            if agent_id in self._capped:
                continue
            if agent_id not in self._waiting:
                return False
            if bus.has_mail(agent_id, a.turn + 1) or bus.next_due_in(agent_id) is not None:
                return False
        return True
