"""`RunStore` on Postgres: one run's events, context messages, and state (docs/event-log.md).

The run's worker is the only writer, so the chain head and each agent's message tail are cached
in memory and advanced only after a commit succeeds. Every write transaction first checks the
run's `owner_epoch`, so a worker that lost the run cannot write to it.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import TypeAdapter
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from swarmeval.db import (
    agent_state,
    checkpoints,
    control_runs,
    deliveries,
    events,
    extension_state,
    messages,
)
from swarmeval.events.seal import ChainHead, seal
from swarmeval.runtime.messages import ChatMessage
from swarmeval.runtime.ports import AgentContext
from swarmeval.runtime.records import (
    CommittedEvent,
    DeliveryChange,
    ExtensionSnapshot,
    MessageDeliverRecord,
    MessageSendRecord,
    Transaction,
)

NOTIFY_CHANNEL = "swarmeval_events"

_MESSAGE = TypeAdapter[ChatMessage](ChatMessage)


class RunNotFoundError(Exception):
    pass


class FencedError(Exception):
    """This worker's `owner_epoch` is stale: another worker owns the run now. Stop the run."""


@dataclass(frozen=True)
class _Tail:
    gen: int
    next_idx: int


class PostgresRunStore:
    """`sandboxes` maps each agent to its sandbox, to attribute tool events. `start` is where
    the chain begins when the run has no event yet: the genesis value, or for a fork its
    source's head at the fork point."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        run_id: str,
        workspace: str,
        owner_epoch: int,
        sandboxes: Mapping[str, str | None],
        start: ChainHead | None = None,
    ) -> None:
        self._engine = engine
        self._start = start or ChainHead.start(run_id)
        self._run_id = run_id
        self._workspace = workspace
        self._epoch = owner_epoch
        self._sandboxes = dict(sandboxes)
        self._head: ChainHead | None = None
        self._tails: dict[str, _Tail] = {}

    @property
    def run_id(self) -> str:
        return self._run_id

    async def commit(self, txn: Transaction) -> list[CommittedEvent]:
        async with self._engine.begin() as conn:
            await self._fence(conn)
            head = self._head or await self._load_head(conn)
            committed, event_rows, head = seal(
                txn.events,
                head,
                run_id=self._run_id,
                workspace=self._workspace,
                sandboxes=self._sandboxes,
            )
            if event_rows:
                await conn.execute(insert(events), event_rows)
            if txn.inherited_mail:
                await conn.execute(
                    insert(deliveries),
                    [
                        {
                            "run_id": self._run_id,
                            "msg_seq": m.send_seq,
                            "recipient": m.recipient,
                            "status": "pending" if m.due_turn is None else "delayed",
                            "due_turn": m.due_turn,
                        }
                        for m in txn.inherited_mail
                    ],
                )
            await self._write_deliveries(conn, committed)
            await self._decide_deliveries(conn, txn.deliveries)
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
                        {
                            "run_id": self._run_id,
                            "instance_id": k,
                            "seq": head.seq,
                            "state": v.state,
                            "rng_uses": v.rng_uses,
                        }
                        for k, v in txn.extension_states.items()
                    ],
                )
            if txn.checkpoint is not None:
                await conn.execute(
                    insert(checkpoints).values(
                        run_id=self._run_id,
                        turn=txn.checkpoint.turn,
                        seq=head.seq,
                        state=txn.checkpoint.model_dump(mode="json"),
                    )
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

    async def extension_states(self) -> dict[str, ExtensionSnapshot]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                select(
                    extension_state.c.instance_id,
                    extension_state.c.state,
                    extension_state.c.rng_uses,
                )
                .ext(distinct_on(extension_state.c.instance_id))
                .where(extension_state.c.run_id == self._run_id)
                .order_by(
                    extension_state.c.instance_id,
                    extension_state.c.seq.desc(),
                    extension_state.c.id.desc(),
                )
            )
            return {
                instance_id: ExtensionSnapshot(state, rng_uses)
                for instance_id, state, rng_uses in rows
            }

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

    async def _write_deliveries(
        self, conn: AsyncConnection, committed: list[CommittedEvent]
    ) -> None:
        """A send opens one pending row per recipient; a delivery closes its row. Both commit
        with the event that describes them, so recovery reads pending mail from the table."""
        for event in committed:
            match event.record:
                case MessageSendRecord(recipients=recipients) if recipients:
                    await conn.execute(
                        insert(deliveries),
                        [
                            {
                                "run_id": self._run_id,
                                "msg_seq": event.seq,
                                "recipient": recipient,
                                "status": "pending",
                            }
                            for recipient in recipients
                        ],
                    )
                case MessageDeliverRecord(send_seq=send_seq, recipient=recipient):
                    result = await conn.execute(
                        update(deliveries)
                        .where(
                            deliveries.c.run_id == self._run_id,
                            deliveries.c.msg_seq == send_seq,
                            deliveries.c.recipient == recipient,
                            deliveries.c.status.in_(("pending", "delayed")),
                        )
                        .values(status="delivered", delivered_seq=event.seq)
                    )
                    if result.rowcount != 1:
                        raise RuntimeError(
                            f"run {self._run_id}: delivery of message {send_seq} to "
                            f"`{recipient}` has no pending or delayed row. A message is "
                            "delivered once, after its send commits, and never after it was "
                            "dropped."
                        )
                case _:
                    pass

    async def _decide_deliveries(
        self, conn: AsyncConnection, changes: list[DeliveryChange]
    ) -> None:
        """`before_deliver` verdicts: only a pending row can be dropped or delayed."""
        for change in changes:
            result = await conn.execute(
                update(deliveries)
                .where(
                    deliveries.c.run_id == self._run_id,
                    deliveries.c.msg_seq == change.send_seq,
                    deliveries.c.recipient == change.recipient,
                    deliveries.c.status == "pending",
                )
                .values(status=change.status, due_turn=change.due_turn)
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"run {self._run_id}: message {change.send_seq} to `{change.recipient}` "
                    f"cannot become `{change.status}`: it has no pending row. A message is "
                    "routed once, after its send commits."
                )

    async def _load_head(self, conn: AsyncConnection) -> ChainHead:
        last = (
            await conn.execute(
                select(events.c.seq, events.c.hash)
                .where(events.c.run_id == self._run_id)
                .order_by(events.c.seq.desc())
                .limit(1)
            )
        ).one_or_none()
        if last is None:
            return self._start
        started_at = (
            self._start.started_at
            or (
                await conn.execute(
                    select(events.c.ts)
                    .where(events.c.run_id == self._run_id)
                    .order_by(events.c.seq)
                    .limit(1)
                )
            ).scalar_one()
        )
        return ChainHead(seq=last.seq, hash=last.hash, started_at=started_at)

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

        for (agent_id, gen), copied in txn.inherited.items():
            tails[agent_id] = _Tail(gen=gen, next_idx=0)
            for message in copied:
                append(agent_id, message)
        for agent_id, initial in txn.new_generations.items():
            current = tails.get(agent_id) or await self._tail(conn, agent_id)
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
