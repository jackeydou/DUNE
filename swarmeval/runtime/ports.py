"""The services the loop depends on, as protocols.

Production implementations talk to Postgres, the model-gateway, sandboxd, and the internet.
Tests use the fakes in `tests/runtime/fakes.py`.
"""

from dataclasses import dataclass
from typing import Protocol

from swarmeval.runtime.messages import ChatMessage, ModelRequest, ModelResponse
from swarmeval.runtime.records import (
    CommittedEvent,
    Exec,
    ExecResult,
    ExtensionSnapshot,
    Transaction,
    WebExchange,
    WebRequest,
)


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

    async def extension_states(self) -> dict[str, ExtensionSnapshot]:
        """Latest committed state per extension instance id."""
        ...


class ModelClient(Protocol):
    async def generate(
        self, caller: Caller, request: ModelRequest, *, parent_id: str | None
    ) -> RecordedResponse:
        """Returns only after the gateway's record of this call has been committed through the
        run's writer, so the returned event is already evidence. `parent_id` is the recorded
        event's causal parent (docs/event-log.md#causal-parents)."""
        ...


class SandboxExecutor(Protocol):
    async def exec(
        self, sandbox_id: str, os_user: str | None, command: Exec, *, call_id: str
    ) -> ExecResult:
        """`call_id` names the tool call, or other caller, that file changes and surviving
        processes are attributed to. Unique within the run."""
        ...


class Pauser(Protocol):
    async def wait(self, reason: str) -> None:
        """Holds the run paused until a person resumes it, or until it is cancelled, then
        returns. The loop checks for a stop after it returns."""
        ...


class WebClient(Protocol):
    async def request(self, request: WebRequest) -> WebExchange:
        """Sends `request` from the worker, only to a public address, and returns once both
        bodies are in the blob store. What the network or the remote does is reported in the
        exchange, not raised."""
        ...
