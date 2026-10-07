"""The loop's state per agent, and turning it into, and back from, a checkpoint
(docs/event-log.md#checkpoints)."""

from collections.abc import Mapping, Sequence
from typing import Literal

from swarmeval.runtime.extensions.api import AgentInfo
from swarmeval.runtime.extensions.interventions import sha256_json
from swarmeval.runtime.fork import (
    ForkStart,
    ReplaceDelivery,
    ReplaceModel,
    check_edits,
    delivery_intervention,
    edit_intervention,
    edited_contexts,
    model_intervention,
    replace_mail,
)
from swarmeval.runtime.records import (
    AgentCheckpoint,
    AgentStateRow,
    Checkpoint,
    EventDraft,
    ExtensionCheckpoint,
    ExtensionSnapshot,
    MailCheckpoint,
    QueuedCheckpoint,
    Transaction,
)
from swarmeval.runtime.specs import AgentSpec

Status = Literal["awaiting_admit", "ready", "finished"]


class AgentRun:
    def __init__(self, spec: AgentSpec) -> None:
        self.spec = spec
        self.info = AgentInfo(
            id=spec.id, model=spec.model, sandbox_id=spec.sandbox_id, tools=spec.tools
        )
        self.gen = 0
        self.length = 0
        self.turn = 0
        self.finished = False
        self.last_input = ""
        """The last event whose content was admitted into this agent's context: the parent of
        its next model call (docs/event-log.md#causal-parents). Set when generation 0 commits."""

    def row(self, status: Status, tokens_used: int) -> AgentStateRow:
        return AgentStateRow(
            agent_id=self.spec.id,
            gen=self.gen,
            length=self.length,
            turn=self.turn,
            status=status,
            tokens_used=tokens_used,
        )


def checkpoint_of(
    *,
    turns: int,
    rest: Sequence[AgentRun],
    tokens_used: int,
    agents: Sequence[AgentRun],
    extensions: dict[str, ExtensionCheckpoint],
    mail: tuple[MailCheckpoint, ...],
    queued: tuple[QueuedCheckpoint, ...],
    spawned: int,
) -> Checkpoint:
    """`rest` is the agents left in the current round, the one about to step first."""
    return Checkpoint(
        turn=turns,
        round=tuple(a.spec.id for a in rest),
        tokens_used=tokens_used,
        agents={
            a.spec.id: AgentCheckpoint(
                gen=a.gen,
                length=a.length,
                turn=a.turn,
                finished=a.finished,
                last_input=a.last_input,
            )
            for a in agents
        },
        extensions=extensions,
        mail=mail,
        queued=queued,
        spawned=spawned,
    )


def fork_start(
    fork: ForkStart, agents: Mapping[str, AgentRun], started: EventDraft
) -> tuple[Transaction, list[MailCheckpoint]]:
    """Puts the agents in their checkpointed state, then applies the edits, each an
    intervention parented to `started`. Returns what to commit with `started`, and the mail to
    carry, with any delivery edit applied. The contexts are copied at their generation numbers,
    so the source's events still explain them."""
    checkpoint = fork.checkpoint
    tokens = checkpoint.tokens_used
    txn = Transaction(events=[started], inherited_mail=list(checkpoint.mail))
    txn.extension_states.update(
        {k: ExtensionSnapshot(v.state, v.rng_uses) for k, v in checkpoint.extensions.items()}
    )
    for agent in agents.values():
        saved = checkpoint.agents[agent.spec.id]
        agent.gen, agent.length, agent.turn = saved.gen, saved.length, saved.turn
        agent.finished, agent.last_input = saved.finished, saved.last_input
        txn.inherited[(agent.spec.id, saved.gen)] = fork.contexts[agent.spec.id]
        txn.agent_states.append(agent.row("finished" if saved.finished else "ready", tokens))
    check_edits(fork.edits, fork.contexts, checkpoint.mail)
    for agent_id, messages in edited_contexts(fork.contexts, fork.edits).items():
        agent = agents[agent_id]
        before = sha256_json([m.model_dump(mode="json") for m in fork.contexts[agent_id]])
        draft = edit_intervention(agent_id, before, messages, started.event_id)
        txn.events.append(draft)
        agent.gen, agent.length, agent.last_input = agent.gen + 1, len(messages), draft.event_id
        # An edited agent takes its turn again, even one that had finished: it reads the edit.
        agent.finished = False
        txn.new_generations[agent_id] = messages
        txn.agent_states.append(agent.row("ready", tokens))
    txn.events.extend(
        model_intervention(e, started.event_id) for e in fork.edits if isinstance(e, ReplaceModel)
    )
    mail = list(checkpoint.mail)
    for edit in fork.edits:
        if isinstance(edit, ReplaceDelivery):
            i = next(i for i, m in enumerate(mail) if replace_mail(m, edit))
            draft = delivery_intervention(
                mail[i], edit, sha256_json(mail[i].content), started.event_id
            )
            txn.events.append(draft)
            mail[i] = mail[i].model_copy(
                update={"content": edit.content, "parent_id": draft.event_id}
            )
    return txn, mail


class Stopped(Exception):
    """Raised at a hook point once a stop was asked for."""

    def __init__(self, reason: str, cause: str | None) -> None:
        self.reason = reason
        self.cause = cause
        """The event that stopped the run, if an event did."""
