from pathlib import Path

import pytest

from swarmeval.core import load_case
from swarmeval.core.models import Display
from tests.core.test_loader import base_case, base_env, load_error, write


def desktop_env(**display: object) -> dict[str, object]:
    env = base_env()
    env["schema_version"] = 2
    env["sandbox_profiles"]["desktop"] = {
        "image": "swarmeval/display:dev",
        "fs": [{"path": "/workspace"}],
        "display": display,
    }
    return env


def test_a_profile_display_defaults_to_xga(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][0].update(tools=["browser", "computer"], sandbox_profile="desktop")

    (variant,) = load_case(
        write(tmp_path, case, desktop_env(url="http://127.0.0.1:8080/"))
    ).variants

    display = variant.env.sandbox_profiles["desktop"].display
    assert display == Display(width=1024, height=768, url="http://127.0.0.1:8080/")
    assert variant.env.sandbox_profiles["default"].display is None


@pytest.mark.parametrize("tool", ["browser", "computer"])
def test_a_display_tool_needs_a_profile_with_a_display(tmp_path: Path, tool: str) -> None:
    case = base_case()
    case["swarm"]["agents"][0]["tools"] = ["shell", tool]

    message = load_error(tmp_path, case, desktop_env())

    assert f"agent `dev` lists `{tool}`" in message
    assert "profile `default`, which has no `display`" in message


@pytest.mark.parametrize("path", ["/run", "/run/swarm-display", "/run/swarm-display/out"])
def test_a_key_path_cannot_overlap_the_display_state(tmp_path: Path, path: str) -> None:
    env = desktop_env()
    env["sandbox_profiles"]["desktop"]["fs"] = [{"path": path}]  # pyright: ignore[reportIndexIssue]

    message = load_error(tmp_path, base_case(), env)

    assert f"key path `{path}` overlaps `/run/swarm-display`" in message


@pytest.mark.parametrize(
    "display", [{"width": 100}, {"height": 5000}, {"url": "http://x/\nInjected: 1"}, {"depth": 24}]
)
def test_a_display_out_of_range_is_rejected(tmp_path: Path, display: dict[str, object]) -> None:
    message = load_error(tmp_path, base_case(), desktop_env(**display))

    assert "display" in message


def test_a_display_needs_env_version_2(tmp_path: Path) -> None:
    env = desktop_env()
    env["schema_version"] = 1

    message = load_error(tmp_path, base_case(), env)

    assert "profile `desktop` has `display`, which `schema_version: 2` added" in message


def test_an_agent_without_a_sandbox_cannot_list_a_display_tool(tmp_path: Path) -> None:
    case = base_case()
    case["schema_version"] = 5
    case["swarm"]["agents"][1].update(tools=["browser"], sandbox="none")

    message = load_error(tmp_path, case, desktop_env())

    assert "agent `qa` has `sandbox: none` but lists `browser`" in message


def test_no_agent_may_run_as_the_display_user(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][0].update(sandbox_profile="desktop", os_user="swarmdisplay")

    message = load_error(tmp_path, case, desktop_env())

    assert "agent `dev` sets `os_user: swarmdisplay`" in message
