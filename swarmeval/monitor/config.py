"""`swarmeval.monitor`'s config and state, apart from the extension so the case loader can check
a config without importing the detectors."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PositiveInt, model_validator

from swarmeval.detect.defs import DetectorDef

MONITOR = "swarmeval.monitor"


class InjectAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: Annotated[str, Field(min_length=1)]
    agent: str | None = None
    """Who gets the message. Default: the agent of the event that triggered the hit; a hit on
    an event no agent caused then injects nothing."""


class MonitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    detectors: Annotated[tuple[DetectorDef, ...], Field(min_length=1)]
    on_hit: Literal["alert", "pause", "stop", "inject"] = "alert"
    """Every hit is an alert. `pause` holds the run for a person (`ResumeRun`), `stop` ends it,
    `inject` puts `inject.content` into an agent's context at its next turn."""
    severity: Literal["low", "medium", "high"] = "high"
    inject: InjectAction | None = None
    max_actions: PositiveInt | None = 1
    """Actions after this many hits are alerts only; `null` for no limit."""

    @model_validator(mode="after")
    def _inject_has_content(self) -> Self:
        if (self.on_hit == "inject") != (self.inject is not None):
            raise ValueError(
                "`inject` is required with `on_hit: inject`, and only then: "
                "write `inject: {content: ...}` or change `on_hit`."
            )
        return self


class MonitorState(BaseModel):
    detectors: list[JsonValue] = Field(default_factory=list[JsonValue])
    """Each detector's state, in config order."""
    hits: int = 0
    actions: int = 0
