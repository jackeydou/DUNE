from pathlib import Path
from typing import Any

import pytest
import yaml

from swarmeval.core import CaseError, load_case, run_spec
from swarmeval.runtime.messages import SystemMessage, UserMessage
from tests.runtime.fakes import call, harness, reply


def base_case() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "demo",
        "workspace": "safety",
        "swarm": {
            "agents": [
                {"id": "dev", "model": "m1", "prompt": "prompts/dev.md", "tools": ["shell"]},
                {"id": "qa", "model": "m1", "prompt": "prompts/qa.md"},
            ],
        },
        "task": {"input": "task.md"},
    }


def base_env() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "sandbox_profiles": {"default": {"image": "swarmeval/agent-base:py312"}},
    }


def write(
    root: Path,
    case: dict[str, Any] | None = None,
    env: dict[str, Any] | None = None,
    files: dict[str, str] | None = None,
) -> Path:
    case_dir = root / "case"
    all_files = {
        "prompts/dev.md": "You are dev.",
        "prompts/qa.md": "You are qa.",
        "task.md": "Fix the bug.",
        **(files or {}),
    }
    for rel, text in all_files.items():
        path = case_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (case_dir / "case.yaml").write_text(yaml.safe_dump(case or base_case(), sort_keys=False))
    (case_dir / "env.yaml").write_text(yaml.safe_dump(env or base_env(), sort_keys=False))
    return case_dir


def load_error(tmp_path: Path, case: dict[str, Any], env: dict[str, Any] | None = None) -> str:
    with pytest.raises(CaseError) as err:
        load_case(write(tmp_path, case, env))
    return str(err.value)


def test_minimal_case_gives_each_agent_a_private_default_sandbox(tmp_path: Path) -> None:
    loaded = load_case(write(tmp_path))

    assert (loaded.id, loaded.workspace, loaded.epochs) == ("demo", "safety", 1)
    (variant,) = loaded.variants
    assert variant.values == {}
    assert [(s.id, s.profile, s.shared, s.agents) for s in variant.sandboxes.values()] == [
        ("dev", "default", False, ("dev",)),
        ("qa", "default", False, ("qa",)),
    ]
    assert variant.prompts["dev"].system == "You are dev."
    assert variant.prompts["qa"].task == "Fix the bug."


def test_shared_sandbox_is_named_by_env_and_private_one_by_agent(tmp_path: Path) -> None:
    case = base_case()
    agents = case["swarm"]["agents"]
    agents[0]["sandbox"] = "team_box"
    agents[1].update(sandbox="team_box", os_user="qa")
    agents.append({"id": "rival", "model": "m1", "prompt": "prompts/qa.md"})
    agents[2]["sandbox_profile"] = "restricted"
    env = base_env()
    env["sandbox_profiles"]["restricted"] = {
        "image": "img",
        "fs": [{"path": "/workspace/tests", "mode": "ro", "protected": True}],
        "limits": {"cpu": 1, "memory": "2gib", "pids": 256},
    }
    env["sandboxes"] = {"team_box": {"profile": "default"}}

    (variant,) = load_case(write(tmp_path, case, env)).variants

    assert variant.sandbox_of("qa").id == "team_box"
    assert variant.sandboxes["team_box"].agents == ("dev", "qa")
    assert variant.sandboxes["team_box"].shared
    assert variant.sandbox_of("rival").profile == "restricted"
    assert variant.env.sandbox_profiles["restricted"].limits.memory == 2 * 2**30


def test_variants_expand_as_cartesian_product_with_typed_substitution(tmp_path: Path) -> None:
    case = base_case()
    case["variants"] = {"model": ["m1", "m2"], "disclosed": [True, False]}
    case["epochs"] = 3
    case["swarm"]["agents"][0]["model"] = "${variant.model}"
    case["swarm"]["agents"][1]["model"] = "${variant.model}-judge"
    case["extensions"] = [{"use": "acme.guard", "config": {"disclosed": "${variant.disclosed}"}}]

    loaded = load_case(write(tmp_path, case))

    assert loaded.epochs == 3
    assert [v.values for v in loaded.variants] == [
        {"model": "m1", "disclosed": True},
        {"model": "m1", "disclosed": False},
        {"model": "m2", "disclosed": True},
        {"model": "m2", "disclosed": False},
    ]
    last = loaded.variants[3].case
    assert last.swarm.agents[0].model == "m2"
    assert last.swarm.agents[1].model == "m2-judge"
    assert last.extensions[0].config == {"disclosed": False}


def test_overrides_replace_an_axis(tmp_path: Path) -> None:
    case = base_case()
    case["variants"] = {"model": ["m1", "m2"]}
    case["swarm"]["agents"][0]["model"] = "${variant.model}"

    loaded = load_case(write(tmp_path, case), overrides={"model": ["m3"]})

    assert [v.case.swarm.agents[0].model for v in loaded.variants] == ["m3"]


def test_override_of_unknown_axis_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="override `temp` is not an axis"):
        load_case(write(tmp_path), overrides={"temp": [0.5]})


def test_env_yaml_is_substituted_too(tmp_path: Path) -> None:
    case = base_case()
    case["variants"] = {"image": ["a", "b"]}
    env = base_env()
    env["sandbox_profiles"]["default"]["image"] = "swarmeval/${variant.image}:1"

    loaded = load_case(write(tmp_path, case, env))

    images = [v.env.sandbox_profiles["default"].image for v in loaded.variants]
    assert images == ["swarmeval/a:1", "swarmeval/b:1"]


def test_limits_accept_suffixed_token_counts(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["limits"] = {"max_turns": 40, "max_tokens": "400k"}

    (variant,) = load_case(write(tmp_path, case)).variants

    assert variant.case.swarm.limits.max_tokens == 400_000


def test_run_spec_maps_agents_prompts_and_sandboxes(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][0].update(sandbox="box", sampling={"temperature": 0.2, "seed": 1})
    case["swarm"]["agents"][1]["task"] = "prompts/qa.md"
    case["swarm"]["limits"] = {"max_turns": 5}
    env = base_env()
    env["sandboxes"] = {"box": {"profile": "default"}}
    (variant,) = load_case(write(tmp_path, case, env)).variants

    spec = run_spec(variant, run_id="run_1", seed=9)

    dev, qa = spec.agents
    assert (dev.sandbox_id, dev.temperature, dev.seed, dev.tools) == ("box", 0.2, 1, ("shell",))
    assert (qa.sandbox_id, qa.system_prompt, qa.task) == ("qa", "You are qa.", "You are qa.")
    assert (spec.run_id, spec.seed, spec.limits.max_turns) == ("run_1", 9, 5)


# Rejections. Each names the file or field and says what to do.


def test_unknown_key_is_rejected_with_its_location(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][0]["role"] = "monitor"

    message = load_error(tmp_path, case)

    assert "case.yaml" in message
    assert "`swarm.agents[0].role`: unknown key" in message


def test_missing_schema_version_is_rejected(tmp_path: Path) -> None:
    case = base_case()
    del case["schema_version"]

    assert "has no `schema_version`. Add `schema_version: 1`" in load_error(tmp_path, case)


def test_unsupported_schema_version_is_rejected(tmp_path: Path) -> None:
    env = base_env()
    env["schema_version"] = 2

    assert "env.yaml has `schema_version: 2`" in load_error(tmp_path, base_case(), env)


def test_workspace_is_required(tmp_path: Path) -> None:
    case = base_case()
    del case["workspace"]

    assert "`workspace`: required, but missing." in load_error(tmp_path, case)


def test_sandbox_and_sandbox_profile_are_exclusive(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][0].update(sandbox="box", sandbox_profile="default")

    assert "sets both `sandbox` and `sandbox_profile`" in load_error(tmp_path, case)


def test_undeclared_shared_sandbox_is_rejected(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][0]["sandbox"] = "team_box"

    assert "uses sandbox `team_box`, which" in load_error(tmp_path, case)


def test_unused_shared_sandbox_is_rejected(tmp_path: Path) -> None:
    env = base_env()
    env["sandboxes"] = {"team_box": {"profile": "default"}}

    assert "declares shared sandboxes `team_box` that no agent uses" in load_error(
        tmp_path, base_case(), env
    )


def test_agent_without_sandbox_needs_a_default_profile(tmp_path: Path) -> None:
    env = base_env()
    env["sandbox_profiles"] = {"restricted": {"image": "img"}}

    message = load_error(tmp_path, base_case(), env)

    assert "agent `dev` needs sandbox profile `default`" in message
    assert "Add a `default` profile" in message


def test_shared_sandbox_named_like_an_agent_is_rejected(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][1]["sandbox"] = "dev"
    env = base_env()
    env["sandboxes"] = {"dev": {"profile": "default"}}

    assert "also declares a shared sandbox `dev`" in load_error(tmp_path, case, env)


def test_shared_sandbox_with_unknown_profile_is_rejected(tmp_path: Path) -> None:
    env = base_env()
    env["sandboxes"] = {"box": {"profile": "missing"}}

    assert "sandbox `box` uses unknown profile `missing`" in load_error(tmp_path, base_case(), env)


def test_channel_member_must_be_an_agent(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["channels"] = [{"id": "dm", "members": ["dev", "seller_c"]}]

    assert "channel `dm` lists unknown member `seller_c`" in load_error(tmp_path, case)


def test_duplicate_agent_ids_are_rejected(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][1]["id"] = "dev"

    assert "agent `dev` appears twice" in load_error(tmp_path, case)


def test_agent_without_task_is_rejected(tmp_path: Path) -> None:
    case = base_case()
    del case["task"]
    case["swarm"]["agents"][0]["task"] = "task.md"

    assert "agents `qa` have no task" in load_error(tmp_path, case)


def test_prompt_outside_the_case_directory_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "secret.md").write_text("host file")
    case = base_case()
    case["swarm"]["agents"][0]["prompt"] = "../secret.md"

    assert "`swarm.agents[dev].prompt` points to `../secret.md`, which is outside" in load_error(
        tmp_path, case
    )


def test_absolute_prompt_path_is_rejected(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][0]["prompt"] = "/etc/passwd"

    assert "`/etc/passwd` is absolute" in load_error(tmp_path, case)


def test_missing_prompt_file_is_rejected(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][0]["prompt"] = "prompts/nope.md"

    assert "`prompts/nope.md`, which does not exist" in load_error(tmp_path, case)


def test_reference_to_undeclared_axis_names_the_field(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][0]["model"] = "${variant.model}"

    message = load_error(tmp_path, case)

    assert "`swarm.agents[0].model` refers to `${variant.model}`" in message
    assert "Declare it under `variants:`" in message


def test_fixed_keys_cannot_vary(tmp_path: Path) -> None:
    case = base_case()
    case["variants"] = {"ws": ["a", "b"]}
    case["workspace"] = "${variant.ws}"

    assert "`workspace` refers to a variant" in load_error(tmp_path, case)


def test_repeated_variant_value_is_rejected(tmp_path: Path) -> None:
    case = base_case()
    case["variants"] = {"model": ["m1", "m1"]}

    assert "variant axis `model`" in load_error(tmp_path, case)


def test_bad_token_count_is_rejected(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["limits"] = {"max_tokens": "lots"}

    assert "`lots` is not a count" in load_error(tmp_path, case)


def test_missing_case_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match=r"does not exist\. A case directory needs `case\.yaml`"):
        load_case(tmp_path)


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    case_dir = write(tmp_path)
    (case_dir / "case.yaml").write_text("id: [unclosed")

    with pytest.raises(CaseError, match="is not valid YAML"):
        load_case(case_dir)


async def test_loaded_case_drives_the_run_loop(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][1]["sandbox"] = "box"
    env = base_env()
    env["sandboxes"] = {"box": {"profile": "default"}}
    (variant,) = load_case(write(tmp_path, case, env)).variants
    spec = run_spec(variant, run_id="run_1", seed=7)
    h = harness(
        spec.agents,
        {"dev": [reply("", call("shell", '{"cmd": "ls"}')), reply("done")], "qa": [reply("ok")]},
    )

    outcome = await h.loop.run()

    assert outcome.status == "finished"
    assert h.sandbox.calls[0][0] == "dev"
    assert h.store.messages("qa")[:2] == [
        SystemMessage(content="You are qa."),
        UserMessage(content="Fix the bug."),
    ]


def test_symlink_leaving_the_case_directory_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "secret.md").write_text("host file")
    case_dir = write(tmp_path)
    (case_dir / "prompts/dev.md").unlink()
    (case_dir / "prompts/dev.md").symlink_to(tmp_path / "secret.md")

    with pytest.raises(CaseError, match="outside the case directory"):
        load_case(case_dir)


@pytest.mark.parametrize(
    "path", ["/", "/workspace/../etc", "/workspace/.", "/workspace/", "//workspace", "/a//b"]
)
def test_mount_path_must_be_clean_and_not_root(tmp_path: Path, path: str) -> None:
    env = base_env()
    env["sandbox_profiles"]["default"]["fs"] = [{"path": path}]

    message = load_error(tmp_path, base_case(), env)

    assert "sandbox_profiles.default.fs[0].path" in message
