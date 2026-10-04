"""Reading a run's stored state for a fork (docs/services/orchestrator.md#forks): its checkpoints,
the agents' contexts at one, the chain head at the fork point, the run's canary tokens, and its
lineage of sources.

A fork's own events start after its source's `fork_seq`; what the source did up to there stays
in the source's rows, which the fork's export, transcript check, and trace read through the
lineage.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import TypeAdapter
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.db import canaries, checkpoints, events, messages, run_specs
from swarmeval.events.seal import ChainHead
from swarmeval.runtime.extensions import CanaryInfo, SandboxCanaryInfo
from swarmeval.runtime.messages import ChatMessage
from swarmeval.runtime.records import Checkpoint

_MESSAGE = TypeAdapter[ChatMessage](ChatMessage)
_CANARIES = TypeAdapter[tuple[CanaryInfo, ...]](tuple[CanaryInfo, ...])
_SANDBOX_CANARIES = TypeAdapter[tuple[SandboxCanaryInfo, ...]](tuple[SandboxCanaryInfo, ...])


class ForkPointError(Exception):
    """The run has no state a fork can start from at that event."""


class ForkEventNotFound(ForkPointError):
    """The run has no such event."""


@dataclass(frozen=True)
class ForkPoint:
    seq: int
    """The last source event the fork goes on from: its checkpoint's seq."""
    checkpoint: Checkpoint
    contexts: dict[str, tuple[ChatMessage, ...]]


async def fork_point(engine: AsyncEngine, run_id: str, event_id: str) -> ForkPoint:
    """The start of the turn `event_id` happened in: the last checkpoint before it. A checkpoint's
    seq is the last event before its turn, so an event with that seq belongs to the turn before
    (M2 spec decision 8)."""
    async with engine.connect() as conn:
        seq = (
            await conn.execute(
                select(events.c.seq).where(events.c.run_id == run_id, events.c.event_id == event_id)
            )
        ).scalar_one_or_none()
    if seq is None:
        raise ForkEventNotFound(f"run `{run_id}` has no event `{event_id}`.")
    return await point_at(engine, run_id, seq - 1, f"event `{event_id}` (seq {seq})")


async def point_at(engine: AsyncEngine, run_id: str, seq: int, what: str = "") -> ForkPoint:
    """The last checkpoint with a seq of at most `seq`, and the contexts it locates."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(checkpoints.c.seq, checkpoints.c.state)
                .where(checkpoints.c.run_id == run_id, checkpoints.c.seq <= seq)
                .order_by(checkpoints.c.seq.desc(), checkpoints.c.turn.desc())
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            raise ForkPointError(
                f"run `{run_id}` has no turn boundary before {what or f'seq {seq + 1}'}: it "
                "comes before the first turn, the run's turn policy is `async`, which takes no "
                "checkpoints, or the run was recorded before forks were possible. Fork at a "
                "later event of a `round_robin` or `event_driven` run."
            )
        checkpoint = Checkpoint.model_validate(row.state)
        if checkpoint.spawned:
            raise ForkPointError(
                f"run `{run_id}` had {checkpoint.spawned} piece(s) of extension background work "
                f"(`ctx.spawn`) running at the start of the turn holding {what or f'seq {seq + 1}'}"
                ", which a fork cannot carry over. Fork at another event."
            )
        contexts: dict[str, tuple[ChatMessage, ...]] = {}
        for agent_id, agent in checkpoint.agents.items():
            stored = await conn.execute(
                select(messages.c.message)
                .where(
                    messages.c.run_id == run_id,
                    messages.c.agent_id == agent_id,
                    messages.c.gen == agent.gen,
                    messages.c.idx < agent.length,
                )
                .order_by(messages.c.idx)
            )
            contexts[agent_id] = tuple(_MESSAGE.validate_python(m) for m in stored.scalars())
    return ForkPoint(seq=row.seq, checkpoint=checkpoint, contexts=contexts)


async def chain_head(engine: AsyncEngine, run_id: str, seq: int) -> ChainHead:
    """Where a fork's chain starts: the source's hash at `seq`, and the source's first
    timestamp, so `working_start` keeps counting from the source's start."""
    async with engine.connect() as conn:
        digest = (
            await conn.execute(
                select(events.c.hash).where(events.c.run_id == run_id, events.c.seq == seq)
            )
        ).scalar_one()
        started_at = (
            await conn.execute(
                select(events.c.ts).where(events.c.run_id == run_id).order_by(events.c.seq).limit(1)
            )
        ).scalar_one()
    return ChainHead(seq=seq, hash=bytes(digest), started_at=started_at)


async def tip_event_id(engine: AsyncEngine, run_id: str, seq: int) -> str:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                select(events.c.event_id).where(events.c.run_id == run_id, events.c.seq == seq)
            )
        ).scalar_one()


@dataclass(frozen=True)
class Ancestor:
    run_id: str
    up_to: int | None
    """The last of its events that counts for the run asked about; `None` for all of them."""


async def lineage(engine: AsyncEngine, run_id: str) -> list[Ancestor]:
    """The run and its sources, oldest first: a fork of a fork lists both sources, each up to
    the event its fork went on from."""
    found: list[Ancestor] = []
    current, up_to = run_id, None
    async with engine.connect() as conn:
        while True:
            found.append(Ancestor(current, up_to))
            row = (
                await conn.execute(
                    select(run_specs.c.forked_from, run_specs.c.fork_seq).where(
                        run_specs.c.run_id == current
                    )
                )
            ).one_or_none()
            # A run the control plane did not plan (one a test writes directly) is no fork.
            if row is None or row.forked_from is None:
                return found[::-1]
            current, up_to = row.forked_from, row.fork_seq


async def save_tokens(
    engine: AsyncEngine,
    run_id: str,
    file_canaries: Sequence[CanaryInfo],
    sandbox_canaries: Sequence[SandboxCanaryInfo],
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            insert(canaries).values(
                run_id=run_id,
                canaries=_CANARIES.dump_python(tuple(file_canaries), mode="json"),
                sandbox_canaries=_SANDBOX_CANARIES.dump_python(
                    tuple(sandbox_canaries), mode="json"
                ),
            )
        )


async def load_tokens(
    engine: AsyncEngine, run_id: str
) -> tuple[tuple[CanaryInfo, ...], tuple[SandboxCanaryInfo, ...]]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(canaries.c.canaries, canaries.c.sandbox_canaries).where(
                    canaries.c.run_id == run_id
                )
            )
        ).one_or_none()
    if row is None:
        raise ForkPointError(
            f"run `{run_id}` has no stored canary tokens; it was recorded before forks were "
            "possible, so a fork cannot plant the same ones."
        )
    return _CANARIES.validate_python(row.canaries), _SANDBOX_CANARIES.validate_python(
        row.sandbox_canaries
    )
