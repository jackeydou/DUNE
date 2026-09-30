"""Chat messages and model requests, as the agent loop sees them.

These are the loop's own view of a conversation. The model-gateway speaks the OpenAI Chat
Completions protocol on the wire; translating to and from it is the gateway client's job.
All models are frozen: a hook that changes one returns a copy (`model_copy(update=...)`), which
is how the dispatcher detects an intervention.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolCall(Frozen):
    id: str
    name: str
    arguments: str
    """Raw JSON text as the model produced it. Parsed only when the tool runs."""


class SystemMessage(Frozen):
    role: Literal["system"] = "system"
    content: str


class UserMessage(Frozen):
    role: Literal["user"] = "user"
    content: str


class AssistantMessage(Frozen):
    role: Literal["assistant"] = "assistant"
    content: str = ""
    reasoning: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


class ToolMessage(Frozen):
    role: Literal["tool"] = "tool"
    tool_call_id: str
    content: str
    is_error: bool = False


ChatMessage = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolMessage,
    Field(discriminator="role"),
]


class ToolSchema(Frozen):
    """A tool as offered to the model."""

    name: str
    description: str
    parameters: dict[str, JsonValue]


class RequestOptions(Frozen):
    """The part of a model request that `before_model_request` hooks may change.

    Messages are deliberately absent: the context is always `messages[gen][:len]`.
    """

    tools: tuple[str, ...]
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    seed: int | None = None


class ModelRequest(Frozen):
    model: str
    messages: tuple[ChatMessage, ...]
    gen: int | None = None
    """The agent context generation `messages` are the first `len(messages)` of. `None` for a
    request not built from an agent's context, such as an extension's own model call."""
    tools: tuple[ToolSchema, ...] = ()
    options: RequestOptions


class Usage(Frozen):
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class ModelResponse(Frozen):
    message: AssistantMessage
    usage: Usage
