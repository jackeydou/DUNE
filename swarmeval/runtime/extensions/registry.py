"""Loading the extensions a case asks for: resolve, validate config, run setup."""

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from swarmeval.runtime.extensions.api import (
    SUPPORTED_API_VERSIONS,
    Extension,
    ExtensionAPI,
    Registrations,
)

ENTRY_POINT_GROUP = "swarmeval.extensions"


class ExtensionLoadError(Exception):
    """A case's `extensions:` entry cannot be loaded."""


class ExtensionUse(BaseModel):
    """One entry of `extensions:` in `case.yaml`."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    use: str
    alias: str | None = Field(default=None, alias="as")
    config: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])

    @property
    def instance_id(self) -> str:
        return self.alias or self.use


@dataclass(frozen=True)
class LoadedExtension:
    instance_id: str
    extension: Extension[Any, Any]
    config: BaseModel
    registrations: Registrations

    def initial_state(self) -> BaseModel:
        try:
            return self.extension.state()
        except ValidationError as err:
            raise ExtensionLoadError(
                f"extension `{self.instance_id}`: its state model "
                f"`{self.extension.state.__name__}` cannot be built without arguments. "
                "Give every state field a default."
            ) from err


def resolve_entry_point(name: str) -> Extension[Any, Any]:
    if name.startswith("case:"):
        raise ExtensionLoadError(
            f"extension `{name}`: loading code from a case directory is not supported yet "
            "(agent loop spec, open question 1). Install it as a package with a "
            f"`{ENTRY_POINT_GROUP}` entry point instead."
        )
    found = entry_points(group=ENTRY_POINT_GROUP, name=name)
    if not found:
        installed = sorted(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP))
        raise ExtensionLoadError(
            f"no extension named `{name}` is installed. "
            f"Installed: {', '.join(installed) or 'none'}. "
            f"Check the name, or install the package that registers it under `{ENTRY_POINT_GROUP}`."
        )
    (entry,) = found
    loaded = entry.load()
    if not isinstance(loaded, Extension):
        raise ExtensionLoadError(
            f"entry point `{name}` ({entry.value}) is not an extension. "
            "Point it at a setup function decorated with `@extension`."
        )
    return loaded  # pyright: ignore[reportUnknownVariableType]


def load_extensions(
    uses: Sequence[ExtensionUse],
    *,
    builtin_tools: Collection[str] = (),
    resolve: Callable[[str], Extension[Any, Any]] = resolve_entry_point,
) -> list[LoadedExtension]:
    """Loads in the order given, which is also the order hooks run in."""
    loaded: list[LoadedExtension] = []
    seen_ids: set[str] = set()
    tool_owners: dict[str, str] = dict.fromkeys(builtin_tools, "the runtime")

    for use in uses:
        instance_id = use.instance_id
        if instance_id in seen_ids:
            raise ExtensionLoadError(
                f"extension instance `{instance_id}` is listed twice. "
                "Give each extra copy its own name with `as:`."
            )
        seen_ids.add(instance_id)

        ext = resolve(use.use)
        if ext.api_version not in SUPPORTED_API_VERSIONS:
            raise ExtensionLoadError(
                f"extension `{use.use}` targets extension API version {ext.api_version}; this "
                f"runtime supports {sorted(SUPPORTED_API_VERSIONS)}. Upgrade one side."
            )
        try:
            config = ext.config.model_validate(use.config)
        except ValidationError as err:
            raise ExtensionLoadError(f"extension `{instance_id}`: invalid config. {err}") from err

        api: ExtensionAPI[Any, Any] = ExtensionAPI(instance_id=instance_id, config=config)
        ext.setup(api)
        api.close()

        for tool in api.registrations.tools:
            owner = tool_owners.get(tool.name)
            if owner is not None:
                raise ExtensionLoadError(
                    f"extension `{instance_id}` registers tool `{tool.name}`, "
                    f"which {owner} already provides. Rename one of them."
                )
            tool_owners[tool.name] = f"extension `{instance_id}`"

        loaded.append(
            LoadedExtension(
                instance_id=instance_id,
                extension=ext,
                config=config,
                registrations=api.registrations,
            )
        )
    return loaded
