import re
from importlib.metadata import EntryPoint, EntryPoints
from typing import Any

import pytest
from pydantic import BaseModel

from swarmeval.runtime.extensions import (
    ENTRY_POINT_GROUP,
    Exec,
    Extension,
    ExtensionAPI,
    ExtensionDefinitionError,
    ExtensionLoadError,
    ExtensionUse,
    HookContext,
    NoConfig,
    NoState,
    extension,
    load_extensions,
    registry,
)
from tests.runtime.fakes import resolver


class GuardConfig(BaseModel):
    protected: list[str]
    mode: str = "block"


@extension(id="t.configured", api_version=1, config=GuardConfig)
def configured(ext: ExtensionAPI[GuardConfig, NoState]) -> None:
    if ext.config.mode == "alert":

        @ext.on("on_run_end")
        async def _(ctx: HookContext[NoState]) -> None:
            pass


def use(name: str, **fields: Any) -> ExtensionUse:
    return ExtensionUse.model_validate({"use": name, **fields})


def test_config_is_validated_and_decides_what_is_registered() -> None:
    (block, alert) = load_extensions(
        [
            use("t.configured", config={"protected": ["tests/"]}),
            use("t.configured", **{"as": "watcher"}, config={"protected": [], "mode": "alert"}),
        ],
        resolve=resolver(configured),
    )

    assert (block.instance_id, alert.instance_id) == ("t.configured", "watcher")
    assert isinstance(block.config, GuardConfig) and block.config.protected == ["tests/"]
    assert block.registrations.handlers == {}
    assert list(alert.registrations.handlers) == ["on_run_end"]


def test_invalid_config_names_the_instance() -> None:
    with pytest.raises(ExtensionLoadError, match=re.escape("`t.configured`: invalid config")):
        load_extensions([use("t.configured", config={})], resolve=resolver(configured))


def test_unknown_keys_in_a_case_entry_are_rejected() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        use("t.configured", options={})


def test_the_same_instance_twice_needs_an_alias() -> None:
    uses = [use("t.configured", config={"protected": []})] * 2
    with pytest.raises(ExtensionLoadError, match="listed twice"):
        load_extensions(uses, resolve=resolver(configured))


@extension(id="t.future", api_version=99)
def future(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    pass


def test_an_unsupported_api_version_is_rejected() -> None:
    with pytest.raises(ExtensionLoadError, match="API version 99"):
        load_extensions([use("t.future")], resolve=resolver(future))


leaked: list[ExtensionAPI[NoConfig, NoState]] = []


@extension(id="t.late", api_version=1)
def late(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    leaked.append(ext)


def test_registering_after_setup_is_an_error() -> None:
    load_extensions([use("t.late")], resolve=resolver(late))

    with pytest.raises(ExtensionDefinitionError, match="after setup returned"):
        leaked[-1].on("on_run_end")


@extension(id="t.typo", api_version=1)
def typo(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    ext.on("before_tool")  # pyright: ignore[reportCallIssue, reportArgumentType]


def test_an_unknown_hook_is_an_error() -> None:
    with pytest.raises(ExtensionDefinitionError, match="unknown hook `before_tool`"):
        load_extensions([use("t.typo")], resolve=resolver(typo))


class Args(BaseModel):
    cmd: str


def make_shell(ext_id: str) -> Extension[NoConfig, NoState]:
    @extension(id=ext_id, api_version=1)
    def setup(ext: ExtensionAPI[NoConfig, NoState]) -> None:
        @ext.tool("shell", args=Args, description="Shell.", runs_in="sandbox")
        def _(args: Args) -> Exec:
            return Exec(argv=("sh", "-c", args.cmd))

    return setup


def test_a_tool_name_clash_is_an_error() -> None:
    first, second = make_shell("t.shell_one"), make_shell("t.shell_two")

    with pytest.raises(ExtensionLoadError, match="which the runtime already provides"):
        load_extensions([use("t.shell_one")], builtin_tools=["shell"], resolve=resolver(first))
    with pytest.raises(
        ExtensionLoadError, match=re.escape("which extension `t.shell_one` already provides")
    ):
        load_extensions([use("t.shell_one"), use("t.shell_two")], resolve=resolver(first, second))


def test_an_invalid_id_is_rejected_at_declaration() -> None:
    with pytest.raises(ExtensionDefinitionError, match="extension id `Bad Id` is invalid"):
        extension(id="Bad Id", api_version=1)


class NeedsValue(BaseModel):
    value: int


@extension(id="t.bad_state", api_version=1, state=NeedsValue)
def bad_state(ext: ExtensionAPI[NoConfig, NeedsValue]) -> None:
    pass


def test_a_state_model_without_defaults_is_rejected() -> None:
    (loaded,) = load_extensions([use("t.bad_state")], resolve=resolver(bad_state))

    with pytest.raises(ExtensionLoadError, match="Give every state field a default"):
        loaded.initial_state()


# Entry points


not_an_extension = object()


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> None:
    eps = EntryPoints(
        [
            EntryPoint(
                name="t.configured",
                value="tests.runtime.test_registry:configured",
                group=ENTRY_POINT_GROUP,
            ),
            EntryPoint(
                name="t.fake",
                value="tests.runtime.test_registry:not_an_extension",
                group=ENTRY_POINT_GROUP,
            ),
        ]
    )

    def entry_points(*, group: str, name: str | None = None) -> EntryPoints:
        return eps.select(group=group, name=name) if name else eps.select(group=group)

    monkeypatch.setattr(registry, "entry_points", entry_points)


@pytest.mark.usefixtures("installed")
def test_extensions_resolve_through_entry_points() -> None:
    (loaded,) = load_extensions([use("t.configured", config={"protected": []})])

    assert loaded.extension is configured


@pytest.mark.usefixtures("installed")
def test_an_unknown_name_lists_what_is_installed() -> None:
    with pytest.raises(ExtensionLoadError, match=re.escape("Installed: t.configured, t.fake")):
        load_extensions([use("t.missing")])


@pytest.mark.usefixtures("installed")
def test_an_entry_point_must_be_an_extension() -> None:
    with pytest.raises(ExtensionLoadError, match="is not an extension"):
        load_extensions([use("t.fake")])


def test_entry_points_alone_do_not_resolve_case_code() -> None:
    with pytest.raises(ExtensionLoadError, match="only `case_resolver`"):
        load_extensions([use("case:extensions/guard.py")])
