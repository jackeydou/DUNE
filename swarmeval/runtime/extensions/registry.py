"""Loading the extensions a case asks for: resolve, validate config, run setup."""

import hashlib
import sys
import types
from collections.abc import Callable, Collection, Mapping, Sequence
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
CASE_CODE_PREFIX = "case:"
"""An extension reference to a Python file in the case directory, such as
`case:extensions/market.py`."""
_CASE_MODULES = "swarmeval_case_code"


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
    if name.startswith(CASE_CODE_PREFIX):
        raise ExtensionLoadError(
            f"extension `{name}` is code from a case directory, which only `case_resolver`, "
            "given the case's code, resolves."
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


def case_resolver(
    code: Mapping[str, str], *, case_id: str, allowed: bool
) -> Callable[[str], Extension[Any, Any]]:
    """Resolves `case:` references from `code` (the loaded variant's `code`), and everything else
    from entry points. Case code runs in the worker's process with the worker's privileges, so
    it is imported only where the deployment sets `allow_case_code`."""

    def resolve(name: str) -> Extension[Any, Any]:
        if not name.startswith(CASE_CODE_PREFIX):
            return resolve_entry_point(name)
        if not allowed:
            raise ExtensionLoadError(
                f"case `{case_id}` loads extension `{name}` from its own directory, and this "
                "deployment does not run case code: it runs inside the worker with the worker's "
                "privileges. Start the control plane and the workers with `--allow-case-code` "
                "to run it, or install the extension as a package."
            )
        return _import_case_code(name, code[name], case_id)

    return resolve


def _import_case_code(name: str, source: str, case_id: str) -> Extension[Any, Any]:
    """One module per distinct source text, so a module imported for an earlier run of the same
    case is reused, and two cases with the same file name do not collide."""
    module_name = f"{_CASE_MODULES}.m{hashlib.sha256(source.encode()).hexdigest()[:24]}"
    module = sys.modules.get(module_name)
    if module is None:
        module = types.ModuleType(module_name)
        filename = f"<case {case_id}>/{name.removeprefix(CASE_CODE_PREFIX)}"
        module.__file__ = filename
        # Registered before running, as importlib does, so pydantic can resolve the module's
        # annotations while its models are built.
        sys.modules[module_name] = module
        try:
            exec(compile(source, filename, "exec"), module.__dict__)
        except Exception as err:
            del sys.modules[module_name]
            raise ExtensionLoadError(
                f"case `{case_id}`: importing extension `{name}` raised "
                f"{type(err).__name__}: {err}. Fix the file in the case directory."
            ) from err
    found: list[Extension[Any, Any]] = [
        v  # pyright: ignore[reportUnknownVariableType]
        for v in vars(module).values()
        if isinstance(v, Extension)
    ]
    if len(found) != 1:
        raise ExtensionLoadError(
            f"case `{case_id}`: `{name}` defines {len(found)} extensions. A `case:` file defines "
            "exactly one setup function decorated with `@extension`."
        )
    return found[0]


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
