"""The gateway's deployment config: backends and the model names cases may use.

Credentials never appear in the file or in a case: a backend names the environment variable
that holds its key.
"""

import os
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, ValidationError, model_validator

ReasoningPassback = Literal["none", "within_turn", "all"]


class GatewayConfigError(Exception):
    """The gateway's config file is missing, malformed, or inconsistent."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Sampling(Strict):
    """Defaults applied where a request leaves a parameter unset."""

    temperature: float | None = None
    top_p: float | None = None
    max_completion_tokens: int | None = None
    seed: int | None = None


class Backend(Strict):
    base_url: str
    api_key_env: str | None = None
    """Environment variable holding the backend's key. Unset for a backend without auth."""
    reasoning_passback: ReasoningPassback = "within_turn"
    """Which past reasoning is sent back upstream: none, only the current turn's (since the last
    user message), or all of it."""
    weights_hash: str | None = None
    """For self-hosted backends: recorded on every call."""
    defaults: Sampling = Sampling()
    timeout_s: PositiveFloat = 600.0
    max_retries: Annotated[int, Field(ge=0)] = 2

    def api_key(self) -> str:
        if self.api_key_env is None:
            # The SDK requires a key; a backend without auth ignores it.
            return "unused"
        try:
            return os.environ[self.api_key_env]
        except KeyError as err:
            raise GatewayConfigError(
                f"backend at {self.base_url}: environment variable `{self.api_key_env}` is not "
                "set. Set it to the backend's API key, or drop `api_key_env` if it needs none."
            ) from err


class ModelRoute(Strict):
    backend: str
    upstream_model: str
    """The name the backend serves the model under."""


class GatewayConfig(Strict):
    backends: dict[str, Backend]
    models: dict[str, ModelRoute]
    """Model name as a case writes it → where it is served."""

    @model_validator(mode="after")
    def _routes_name_backends(self) -> Self:
        for name, route in self.models.items():
            if route.backend not in self.backends:
                raise ValueError(
                    f"model `{name}` routes to unknown backend `{route.backend}`. "
                    f"Backends: {', '.join(sorted(self.backends)) or 'none'}."
                )
        return self

    @classmethod
    def load(cls, path: Path) -> Self:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            return cls.model_validate(raw)
        except (OSError, yaml.YAMLError, ValidationError) as err:
            raise GatewayConfigError(f"model-gateway config {path}: {err}") from err
