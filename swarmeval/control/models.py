"""The models runs may use: what model-gateway serves (spec/2026-10-06-run-time-models
decisions 5 and 6). The control plane asks on every submission, so a name the gateway does not
serve is refused before any run is queued."""

from collections.abc import Mapping, Sequence
from typing import Protocol

import httpx2
from pydantic import BaseModel, ValidationError

from swarmeval.core import LoadedCase


class GatewayUnavailable(Exception):
    """model-gateway could not say what it serves."""


class UnknownModel(Exception):
    """A submission names a model model-gateway does not serve."""


class ModelCatalog(Protocol):
    async def served(self) -> tuple[str, ...]:
        """Sorted. Raises `GatewayUnavailable`."""
        ...


class _Model(BaseModel):
    id: str


class _Models(BaseModel):
    data: list[_Model]


class GatewayModels:
    """model-gateway's `GET /v1/models`, asked each time: adding a model to the gateway's config
    makes it available without restarting the control plane."""

    def __init__(self, http: httpx2.AsyncClient) -> None:
        self._http = http

    async def served(self) -> tuple[str, ...]:
        try:
            response = await self._http.get("/v1/models")
            response.raise_for_status()
            listed = _Models.model_validate_json(response.content)
        except httpx2.HTTPError as err:
            raise GatewayUnavailable(
                f"model-gateway at {self._http.base_url} did not list its models: {err}. "
                "Check that it is running and accepts the control plane's certificate."
            ) from err
        except ValidationError as err:
            raise GatewayUnavailable(
                f"model-gateway at {self._http.base_url} answered /v1/models with something "
                f"other than a model list: {err}"
            ) from err
        return tuple(sorted({m.id for m in listed.data}))


def check_served(served: Sequence[str], loaded: LoadedCase) -> None:
    """Every model `loaded` runs, chosen or written in the case, is one model-gateway serves.
    Raises `UnknownModel` naming the first that is not."""
    for slot, names in loaded.models.items():
        for name in names:
            if name not in served:
                raise UnknownModel(
                    f"case `{loaded.label}`: slot `{slot}` is to run on model `{name}`, which "
                    f"model-gateway does not serve. {_served(served)}"
                )
    for name in loaded.fixed_models:
        if name not in served:
            raise UnknownModel(
                f"case `{loaded.label}` itself uses model `{name}` (a `paraphrase` "
                f"intervention's `model`), which model-gateway does not serve. Add it to the "
                f"gateway's config, or change the case. {_served(served)}"
            )


def check_replacements(served: Sequence[str], run_id: str, models: Mapping[str, str]) -> None:
    for slot, name in models.items():
        if name not in served:
            raise UnknownModel(
                f"the fork of run `{run_id}` would run slot `{slot}` on model `{name}`, which "
                f"model-gateway does not serve. {_served(served)}"
            )


def _served(served: Sequence[str]) -> str:
    return f"It serves: {', '.join(served)}." if served else "It serves no models."
