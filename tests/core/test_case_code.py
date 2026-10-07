"""`case:` extension references: read at load, imported only where the deployment allows."""

import json
from pathlib import Path
from typing import Any

import pytest

from swarmeval.core import CaseError, load_case
from swarmeval.runtime.extensions import (
    ExtensionLoadError,
    ExtensionUse,
    case_resolver,
    load_extensions,
)
from swarmeval.runtime.tools import BUILTIN_TOOL_NAMES
from tests.core.test_loader import base_case, write

GUARD = """\
from pydantic import BaseModel

from swarmeval.runtime.extensions import ExtensionAPI, HookContext, extension


class Config(BaseModel):
    word: str


class Args(BaseModel):
    text: str


@extension(id="demo.echo", api_version=1, config=Config)
def setup(ext: ExtensionAPI[Config, BaseModel]) -> None:
    @ext.tool("echo", args=Args, description="Echo.", runs_in="worker")
    async def echo(ctx: HookContext[BaseModel], args: Args) -> str:
        return f"{ext.config.word}: {args.text}"
"""

SECOND = """

@extension(id="demo.again", api_version=1)
def again(ext: ExtensionAPI) -> None: ...
"""


def case_with_code() -> dict[str, Any]:
    case = base_case()
    case["extensions"] = [
        {"use": "case:extensions/echo.py", "as": "echo", "config": {"word": "heard"}}
    ]
    return case


def test_case_code_is_read_into_the_variant(tmp_path: Path) -> None:
    loaded = load_case(write(tmp_path, case_with_code(), files={"extensions/echo.py": GUARD}))

    (variant,) = loaded.variants
    assert variant.code == {"case:extensions/echo.py": GUARD}


def test_case_code_must_be_a_python_file_inside_the_case(tmp_path: Path) -> None:
    case = case_with_code()
    case["extensions"][0]["use"] = "case:extensions/echo.txt"
    with pytest.raises(CaseError, match=r"does not name a Python file"):
        load_case(write(tmp_path / "a", case, files={"extensions/echo.txt": GUARD}))

    case["extensions"][0]["use"] = "case:../outside.py"
    with pytest.raises(CaseError, match=r"outside the case directory"):
        load_case(write(tmp_path / "b", case))

    case["extensions"][0]["use"] = "case:extensions/missing.py"
    with pytest.raises(CaseError, match=r"does not exist"):
        load_case(write(tmp_path / "c", case))


def test_allowed_case_code_loads_and_runs_setup(tmp_path: Path) -> None:
    (variant,) = load_case(
        write(tmp_path, case_with_code(), files={"extensions/echo.py": GUARD})
    ).variants

    loaded = load_extensions(
        variant.extensions,
        builtin_tools=BUILTIN_TOOL_NAMES,
        resolve=case_resolver(variant.code, case_id="demo", allowed=True),
    )

    (echo,) = loaded
    assert echo.instance_id == "echo"
    assert echo.extension.id == "demo.echo"
    assert [t.name for t in echo.registrations.tools] == ["echo"]


def test_the_same_source_is_imported_once(tmp_path: Path) -> None:
    code = {"case:extensions/echo.py": GUARD}
    first = case_resolver(code, case_id="demo", allowed=True)("case:extensions/echo.py")
    second = case_resolver(code, case_id="other", allowed=True)("case:extensions/echo.py")

    assert first is second


def test_case_code_is_refused_unless_allowed() -> None:
    resolve = case_resolver({"case:x.py": GUARD}, case_id="demo", allowed=False)

    with pytest.raises(ExtensionLoadError, match=r"--allow-case-code"):
        resolve("case:x.py")


def test_entry_points_still_resolve_beside_case_code() -> None:
    resolve = case_resolver({}, case_id="demo", allowed=False)

    assert resolve("swarmeval.canary").id == "swarmeval.canary"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("raise RuntimeError('boom')", r"importing extension `case:x.py` raised RuntimeError"),
        ("x = 1", r"defines 0 extensions"),
        (
            GUARD + SECOND,
            r"defines 2 extensions",
        ),
    ],
)
def test_a_broken_case_file_is_refused_naming_it(source: str, message: str) -> None:
    resolve = case_resolver({"case:x.py": source}, case_id="demo", allowed=True)

    with pytest.raises(ExtensionLoadError, match=message):
        resolve("case:x.py")


def test_event_value_scorer_needs_a_threshold(tmp_path: Path) -> None:
    case = base_case()
    scorer: dict[str, Any] = {
        "id": "high",
        "type": "event_value",
        "event": "market.round",
        "field": "mean_index",
        "meaning": "prices were high",
    }
    case["scorers"] = [scorer]
    with pytest.raises(CaseError, match=r"`scorers\[0\]\.event_value\.threshold`: required"):
        load_case(write(tmp_path / "a", case))

    scorer["threshold"] = 0.5
    (variant,) = load_case(write(tmp_path / "b", case)).variants
    assert variant.case.scorers[0].type == "event_value"


def test_the_collusion_pricing_case_loads_its_market() -> None:
    loaded = load_case(Path(__file__).parents[2] / "cases" / "collusion_pricing")

    assert [json.dumps(v.values["paraphrased"]) for v in loaded.variants] == ["[]", '["dm_ab"]']
    for variant in loaded.variants:
        extensions = load_extensions(
            variant.extensions,
            builtin_tools=BUILTIN_TOOL_NAMES,
            resolve=case_resolver(variant.code, case_id=loaded.id, allowed=True),
        )
        assert [e.instance_id for e in extensions] == ["market", "swarmeval.bus.paraphrase"]
        assert [use.config["channels"] for use in variant.extensions[1:]] == [
            list(variant.values["paraphrased"])  # pyright: ignore[reportArgumentType]
        ]
    assert isinstance(loaded.variants[0].extensions[0], ExtensionUse)
