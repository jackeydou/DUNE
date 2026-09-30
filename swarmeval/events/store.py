"""`RunStore` on Postgres: one run's events, context messages, and state (docs/event-log.md).

The run's worker is the only writer, so the chain head and each agent's message tail are cached
in memory and advanced only after a commit succeeds. Every write transaction first checks the
run's `owner_epoch`, so a worker that lost the run cannot write to it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import rfc8785
from pydantic import JsonValue, TypeAdapter
from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from swarmeval.db import agent_state, control_runs, events, extension_state, messages
from swarmeval.events.chain import genesis, link
from swarmeval.events.convert import Attribution, event_type, source_of, to_event
from swarmeval.runtime.messages import ChatMessage
from swarmeval.runtime.ports import AgentContext
from swarmeval.runtime.records import (
    CommittedEvent,
    EventDraft,
    SandboxExecRecord,
    ToolCallRecord,
    Transaction,
)

NOTIFY_CHANNEL = "swarmeval_events"

_MESSAGE = TypeAdapter[ChatMessage](ChatMessage)


class RunNotFoundError(Exception):
    pass


class FencedError(Exception):
    """This worker's `owner_epoch` is stale: another worker owns the run now. Stop the run."""


@dataclass(frozen=True)
class _Head:
    seq: int
    hash: bytes
    started_at: datetime | None
    """Timestamp of seq 1, the zero point of Inspect's `working_start`."""


@dataclass(frozen=True)
class _Tail:
    gen: int
    next_idx: int


class PostgresRunStore:
    """`sandboxes` maps each agent to its sandbox, to attribute tool events."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        run_id: str,
        workspace: str,
        owner_epoch: int,
        sandboxes: Mapping[str, str | None],
    ) -> None:
        self._engine = engine
        self._run_id = run_id
        self._workspace = workspace
        self._epoch = owner_epoch
        self._sandboxes = dict(sandboxes)
        self._head: _Head | None = None
        self._tails: dict[str, _Tail] = {}

    @property
    def run_id(self) -> str:
        return self._run_id

    async def commit(self, txn: Transaction) -> list[CommittedEvent]:
        async with self._engine.begin() as conn:
            await self._fence(conn)
            head = self._head or await self._load_head(conn)
            committed, event_rows, head = self._build_events(txn.events, head)
            if event_rows:
                await conn.execute(insert(events), event_rows)
            tails = await self._write_messages(conn, txn, head.seq)
            if txn.agent_states:
                await conn.execute(
                    insert(agent_state),
                    [
                        {
                            "run_id": self._run_id,
                            "agent_id": row.agent_id,
                            "seq": head.seq,
                            "gen": row.gen,
                            "len": row.length,
                            "turn": row.turn,
                            "status": row.status,
                            "tokens_used": row.tokens_used,
                        }
                        for row in txn.agent_states
                    ],
                )
            if txn.extension_states:
                await conn.execute(
                    insert(extension_state),
                    [
                        {"run_id": self._run_id, "instance_id": k, "seq": head.seq, "state": v}
                        for k, v in txn.extension_states.items()
                    ],
                )
            if event_rows:
                await conn.execute(
                    text("SELECT pg_notify(:channel, :run_id)"),
                    {"channel": NOTIFY_CHANNEL, "run_id": self._run_id},
                )
        self._head = head
        self._tails.update(tails)
        return committed

    async def context(self, agent_id: str) -> AgentContext | None:
        async with self._engine.connect() as conn:
            state = (
                await conn.execute(
                    select(agent_state.c.gen, agent_state.c.len)
                    .where(agent_state.c.run_id == self._run_id)
                    .where(agent_state.c.agent_id == agent_id)
                    .order_by(agent_state.c.seq.desc(), agent_state.c.id.desc())
                    .limit(1)
                )
            ).one_or_none()
            if state is None:
                return None
            gen, length = state
            rows = await conn.execute(
                select(messages.c.message)
                .where(messages.c.run_id == self._run_id)
                .where(messages.c.agent_id == agent_id)
                .where(messages.c.gen == gen)
                .where(messages.c.idx < length)
                .order_by(messages.c.idx)
            )
            loaded = tuple(_MESSAGE.validate_python(m) for m in rows.scalars())
        if len(loaded) != length:
            raise RuntimeError(
                f"run {self._run_id}: agent `{agent_id}` state says generation {gen} has "
                f"{length} messages, but the store holds {len(loaded)}. The run's rows are "
                "inconsistent; do not resume it."
            )
        return AgentContext(gen=gen, messages=loaded)

    async def extension_states(self) -> dict[str, JsonValue]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(extension_state.c.instance_id, extension_state.c.state)
                .ext(distinct_on(extension_state.c.instance_id))
                .where(extension_state.c.run_id == self._run_id)
                .order_by(
                    extension_state.c.instance_id,
                    extension_state.c.seq.desc(),
                    extension_state.c.id.desc(),
                )
            )
            return {instance_id: state for instance_id, state in rows}

    async def _fence(self, conn: AsyncConnection) -> None:
        epoch = (
            await conn.execute(
                select(control_runs.c.owner_epoch)
                .where(control_runs.c.run_id == self._run_id)
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if epoch is None:
            raise RunNotFoundError(
                f"run {self._run_id} has no row in control.runs. Runs are created by the "
                "control plane before a worker writes to them."
            )
        if epoch != self._epoch:
            raise FencedError(
                f"run {self._run_id} is at owner_epoch {epoch}, this worker holds {self._epoch}. "
                "Another worker has taken the run over; stop executing it."
            )

    async def _load_head(self, conn: AsyncConnection) -> _Head:
        last = (
            await conn.execute(
                select(events.c.seq, events.c.hash)
                .where(events.c.run_id == self._run_id)
                .order_by(events.c.seq.desc())
                .limit(1)
            )
        ).one_or_none()
        if last is None:
            return _Head(seq=0, hash=genesis(self._run_id), started_at=None)
        started_at = (
            await conn.execute(
                select(events.c.ts).where(events.c.run_id == self._run_id, events.c.seq == 1)
            )
        ).scalar_one()
        return _Head(seq=last.seq, hash=last.hash, started_at=started_at)

    def _build_events(
        self, drafts: list[EventDraft], head: _Head
    ) -> tuple[list[CommittedEvent], list[dict[str, object]], _Head]:
        committed: list[CommittedEvent] = []
        rows: list[dict[str, object]] = []
        for draft in drafts:
            seq = head.seq + 1
            sandbox_id = self._sandbox_of(draft)
            event = to_event(
                draft.record,
                Attribution(
                    workspace=self._workspace,
                    seq=seq,
                    parent_id=draft.parent_id,
                    agent_id=draft.agent_id,
                    sandbox_id=sandbox_id,
                    extension=draft.extension,
                ),
            )
            started_at = head.started_at or event.timestamp
            event.working_start = (event.timestamp - started_at).total_seconds()
            payload: JsonValue = event.model_dump(mode="json", exclude_none=True)
            try:
                digest = link(head.hash, seq, payload)
            except rfc8785.CanonicalizationError as err:
                raise ValueError(
                    f"run {self._run_id}: the {draft.record.kind} event at seq {seq} (agent "
                    f"{draft.agent_id}, extension {draft.extension}) carries a value JSON cannot "
                    f"represent exactly: {err}. The component that produced it must send finite "
                    "floats and integers within ±(2**53 - 1)."
                ) from err
            assert event.uuid is not None, "Inspect assigns a uuid at construction"
            rows.append(
                {
                    "run_id": self._run_id,
                    "seq": seq,
                    "event_id": event.uuid,
                    "ts": event.timestamp,
                    "type": event_type(event),
                    "source": source_of(draft.record),
                    "agent_id": draft.agent_id,
                    "sandbox_id": sandbox_id,
                    "parent_id": draft.parent_id,
                    "prev_hash": head.hash,
                    "hash": digest,
                    "payload": payload,
                }
            )
            committed.append(
                CommittedEvent(
                    event_id=event.uuid,
                    seq=seq,
                    agent_id=draft.agent_id,
                    extension=draft.extension,
                    parent_id=draft.parent_id,
                    record=draft.record,
                )
            )
            head = _Head(seq=seq, hash=digest, started_at=started_at)
        return committed, rows, head

    def _sandbox_of(self, draft: EventDraft) -> str | None:
        match draft.record:
            case SandboxExecRecord(sandbox_id=sandbox_id):
                return sandbox_id
            case ToolCallRecord(exec_result=result) if result is not None:
                assert draft.agent_id is not None, "the loop attributes every tool call"
                return self._sandboxes[draft.agent_id]
            case _:
                return None

    async def _write_messages(
        self, conn: AsyncConnection, txn: Transaction, seq: int
    ) -> dict[str, _Tail]:
        """Returns the new tails; the caller installs them once the transaction commits."""
        tails: dict[str, _Tail] = {}
        rows: list[dict[str, object]] = []

        def append(agent_id: str, message: ChatMessage) -> None:
            tail = tails[agent_id]
            rows.append(
                {
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "gen": tail.gen,
                    "idx": tail.next_idx,
                    "seq": seq,
                    "message": _MESSAGE.dump_python(message, mode="json"),
                }
            )
            tails[agent_id] = _Tail(gen=tail.gen, next_idx=tail.next_idx + 1)

        for agent_id, initial in txn.new_generations.items():
            current = await self._tail(conn, agent_id)
            tails[agent_id] = _Tail(gen=0 if current is None else current.gen + 1, next_idx=0)
            for message in initial:
                append(agent_id, message)
        for agent_id, message in txn.messages:
            if agent_id not in tails:
                current = await self._tail(conn, agent_id)
                if current is None:
                    raise RuntimeError(
                        f"run {self._run_id}: a message for agent `{agent_id}` was committed "
                        "before the agent had a generation. The loop starts generation 0 first."
                    )
                tails[agent_id] = current
            append(agent_id, message)
        if rows:
            await conn.execute(insert(messages), rows)
        return tails

    async def _tail(self, conn: AsyncConnection, agent_id: str) -> _Tail | None:
        cached = self._tails.get(agent_id)
        if cached is not None:
            return cached
        row = (
            await conn.execute(
                select(messages.c.gen, func.count())
                .where(messages.c.run_id == self._run_id, messages.c.agent_id == agent_id)
                .group_by(messages.c.gen)
                .order_by(messages.c.gen.desc())
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            return None
        gen, count = row
        return _Tail(gen=gen, next_idx=count)
