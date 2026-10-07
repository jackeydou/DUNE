"""`sandbox: none` (case version 5): agents without a sandbox, and cases without an env file."""

import re
from pathlib import Path
from typing import Any

import pytest

from swarmeval.core import CaseError, load_case, run_spec
from tests.core.test_loader import base_case, base_env, load_error, write


def v5(*sandboxless: str) -> dict[str, Any]:
    case = base_case()
    case["schema_version"] = 5
    for agent in case["swarm"]["agents"]:
        if agent["id"] in sandboxless:
            agent["sandbox"] = "none"
            agent["tools"] = [t for t in agent.get("tools", []) if t != "shell"]
    return case


def test_an_agent_with_sandbox_none_gets_no_sandbox_and_no_sandbox_id(tmp_path: Path) -> None:
    loaded = load_case(write(tmp_path, v5("qa")), models={"default": ["m1"]})

    (variant,) = loaded.variants
    assert list(variant.sandboxes) == ["dev"]
    assert variant.sandbox_of("qa") is None
    spec = run_spec(variant, run_id="r", seed=0)
    assert {a.id: a.sandbox_id for a in spec.agents} == {"dev": "dev", "qa": None}


def test_a_case_without_sandboxes_needs_no_env_file(tmp_path: Path) -> None:
    case_dir = write(tmp_path, v5("dev", "qa"))
    (case_dir / "env.yaml").unlink()

    (variant,) = load_case(case_dir).variants

    assert variant.sandboxes == {}
    assert variant.env.sandbox_profiles == {}
    assert variant.files == {}


def test_a_case_without_sandboxes_still_checks_an_env_file_it_has(tmp_path: Path) -> None:
    env = base_env()
    env["canaries"] = [{"id": "key", "sandbox": "dev", "path": "/w/k", "template": "{{canary}}"}]

    message = load_error(tmp_path, v5("dev", "qa"), env)

    assert "canary `key` goes in sandbox `dev`, which no agent uses" in message


def test_an_env_file_the_case_names_must_exist_even_without_sandboxes(tmp_path: Path) -> None:
    case = v5("dev", "qa")
    case["environment"] = "envs/missing.yaml"

    message = load_error(tmp_path, case)

    assert "`environment` points to `envs/missing.yaml`, which does not exist" in message


def test_a_sandboxed_agent_still_needs_the_env_file(tmp_path: Path) -> None:
    case_dir = write(tmp_path, v5("qa"))
    (case_dir / "env.yaml").unlink()

    with pytest.raises(CaseError, match=re.escape("`env.yaml`, which does not exist")):
        load_case(case_dir)


def test_no_default_profile_is_needed_when_only_sandboxless_agents_would_use_it(
    tmp_path: Path,
) -> None:
    case = v5("qa")
    case["swarm"]["agents"][0]["sandbox_profile"] = "box"
    env = {"schema_version": 1, "sandbox_profiles": {"box": {"image": "img"}}}

    (variant,) = load_case(write(tmp_path, case, env)).variants

    assert [(s.id, s.profile) for s in variant.sandboxes.values()] == [("dev", "box")]


def test_a_missing_default_profile_suggests_sandbox_none(tmp_path: Path) -> None:
    env = {"schema_version": 1, "sandbox_profiles": {"box": {"image": "img"}}}

    message = load_error(tmp_path, v5(), env)

    assert "or `sandbox: none` if it runs no commands" in message


def test_a_sandboxless_agent_cannot_list_shell_or_set_an_os_user(tmp_path: Path) -> None:
    case = v5("qa")
    case["swarm"]["agents"][1]["tools"] = ["shell", "send_message"]
    message = load_error(tmp_path, case)
    assert "agent `qa` has `sandbox: none` but lists `shell`" in message

    case = v5("qa")
    case["swarm"]["agents"][1]["os_user"] = "qa"
    message = load_error(tmp_path / "user", case)
    assert "agent `qa` has `sandbox: none` but sets `os_user: qa`" in message


def test_none_names_a_shared_instance_in_version_4(tmp_path: Path) -> None:
    case = base_case()
    for agent in case["swarm"]["agents"]:
        agent["sandbox"] = "none"
    env = base_env()
    env["sandboxes"] = {"none": {"profile": "default"}}

    (variant,) = load_case(write(tmp_path, case, env)).variants

    assert variant.sandboxes["none"].agents == ("dev", "qa")


def test_a_shared_instance_named_none_is_refused_in_version_5(tmp_path: Path) -> None:
    env = base_env()
    env["sandboxes"] = {"none": {"profile": "default"}}

    message = load_error(tmp_path, v5("qa"), env)

    assert "`none` cannot be one" in message
    assert "`sandbox: none` means no sandbox" in message


def test_cross_sandbox_counts_a_sandboxless_agent_as_outside(tmp_path: Path) -> None:
    case = v5("qa")
    case["scorers"] = [{"id": "crossed", "type": "cross_sandbox"}]

    (variant,) = load_case(write(tmp_path, case)).variants

    assert list(variant.sandboxes) == ["dev"]
    case = v5("dev", "qa")
    case["scorers"] = [{"id": "crossed", "type": "cross_sandbox"}]
    assert "but no agent has a sandbox" in load_error(tmp_path / "none", case)
