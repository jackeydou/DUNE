"""The services the loop depends on, as protocols.

Production implementations talk to Postgres, the model-gateway, and sandboxd. Tests use the
fakes in `tests/runtime/fakes.py`.
"""

from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue

from swarmeval.runtime.messages import ChatMessage, ModelRequest, ModelResponse
from swarmeval.runtime.records import CommittedEvent, Exec, ExecResult, Transaction


@dataclass(frozen=True)
class AgentCaller:
    agent_id: str


@dataclass(frozen=True)
class ExtensionCaller:
    instance_id: str


Caller = AgentCaller | ExtensionCaller


@dataclass(frozen=True)
class RecordedResponse:
    response: ModelResponse
    event: CommittedEvent


@dataclass(frozen=True)
class AgentContext:
    gen: int
    messages: tuple[ChatMessage, ...]
    """The first `len` messages of generation `gen`: exactly what the next request sends."""


class RunStore(Protocol):
    """Persistence for one run. The run's worker is its only writer."""

    async def commit(self, txn: Transaction) -> list[CommittedEvent]:
        """Commit atomically. Assigns `seq` and `event_id` to each event, in order."""
        ...

    async def context(self, agent_id: str) -> AgentContext | None:
        """`None` when the agent has no generation yet."""
        ...

    async def extension_states(self) -> dict[str, JsonValue]:
        """Latest committed state per extension instance id."""
        ...


class ModelClient(Protocol):
    async def generate(self, caller: Caller, request: ModelRequest) -> RecordedResponse:
        """Returns only after the gateway's record of this call has been committed through the
        run's writer, so the returned event is already evidence."""
        ...


class SandboxExecutor(Protocol):
    async def exec(self, sandbox_id: str, os_user: str | None, command: Exec) -> ExecResult: ...
