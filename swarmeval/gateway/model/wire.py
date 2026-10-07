"""The subset of the OpenAI Chat Completions protocol spoken between the worker and the gateway.

Only fields SwarmEval uses are accepted; a request with anything else is refused. Reasoning
travels as `reasoning_content` on assistant messages, the vLLM and SGLang convention.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WireFunctionCall(Wire):
    name: str
    arguments: str


class WireToolCall(Wire):
    id: str
    type: Literal["function"] = "function"
    function: WireFunctionCall


class WireSystemMessage(Wire):
    role: Literal["system"] = "system"
    content: str


class WireUserMessage(Wire):
    role: Literal["user"] = "user"
    content: str


class WireAssistantMessage(Wire):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[WireToolCall] | None = None


class WireTextPart(Wire):
    type: Literal["text"] = "text"
    text: str


class WireImageURL(Wire):
    url: Annotated[str, Field(pattern=r"^data:image/png;base64,[A-Za-z0-9+/]+={0,2}$")]
    """A PNG inline as a `data:` URL; the gateway fetches nothing."""


class WireImagePart(Wire):
    type: Literal["image_url"] = "image_url"
    image_url: WireImageURL


WirePart = Annotated[WireTextPart | WireImagePart, Field(discriminator="type")]


class WireToolMessage(Wire):
    role: Literal["tool"] = "tool"
    tool_call_id: str
    content: str | list[WirePart]
    """Parts carry the images a tool returned. Chat Completions takes only text in a tool
    message, so the gateway moves the images into a user message for the backend
    (`upstream.move_tool_images`)."""


WireMessage = Annotated[
    WireSystemMessage | WireUserMessage | WireAssistantMessage | WireToolMessage,
    Field(discriminator="role"),
]


class WireFunction(Wire):
    name: str
    description: str
    parameters: dict[str, JsonValue]


class WireTool(Wire):
    type: Literal["function"] = "function"
    function: WireFunction


class ChatRequest(Wire):
    model: str
    messages: list[WireMessage] = Field(min_length=1)
    tools: list[WireTool] | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_completion_tokens: int | None = None
    seed: int | None = None
    stream: Literal[False] = False


class WireUsage(Wire):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class WireChoice(Wire):
    index: int = 0
    message: WireAssistantMessage
    finish_reason: str | None


class ChatResponse(Wire):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[WireChoice] = Field(min_length=1, max_length=1)
    usage: WireUsage
    system_fingerprint: str | None = None


class ErrorBody(Wire):
    code: str
    message: str


class ErrorResponse(Wire):
    error: ErrorBody


CALL_ID_HEADER = "X-SwarmEval-Call-Id"
"""The worker's id for a call. The gateway echoes it in the call's record."""
