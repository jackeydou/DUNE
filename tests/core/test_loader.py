import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from swarmeval.core import CaseError, choose_models, load_case, run_spec
from swarmeval.runtime.messages import SystemMessage, UserMessage
from tests.runtime.fakes import call, harness, reply


def base_case() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "id": "demo",
        "workspace": "safety",
        "swarm": {
            "agents": [
                {"id": "dev", "prompt": "prompts/dev.md", "tools": ["shell"]},
                {"id": "qa", "prompt": "prompts/qa.md"},
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
    agents.append({"id": "rival", "prompt": "prompts/qa.md"})
    agents[2]["sandbox_profile"] = "restricted"
    env = base_env()
    env["sandbox_profiles"]["restricted"] = {
        "image": "img",
        "fs": [{"path": "/workspace/tests", "mode": "ro", "protected": True}],
        "limits": {"cpu": 1, "memory": "2gib", "pids": 256},
    }
    env["sandboxes"] = {"team_box": {"profile": "default"}}

    (variant,) = load_case(write(tmp_path, case, env)).variants

    assert variant.sandbox_of("qa") == variant.sandboxes["team_box"]
    assert variant.sandboxes["team_box"].agents == ("dev", "qa")
    assert variant.sandboxes["team_box"].shared
    assert variant.sandboxes["rival"].profile == "restricted"
    assert variant.env.sandbox_profiles["restricted"].limits.memory == 2 * 2**30


def test_variants_expand_as_cartesian_product_with_typed_substitution(tmp_path: Path) -> None:
    case = base_case()
    case["variants"] = {"persona": ["dev", "qa"], "disclosed": [True, False]}
    case["epochs"] = 3
    case["swarm"]["agents"][0]["prompt"] = "prompts/${variant.persona}.md"
    case["swarm"]["agents"][1]["os_user"] = "${variant.persona}_user"
    case["extensions"] = [{"use": "acme.guard", "config": {"disclosed": "${variant.disclosed}"}}]

    loaded = load_case(write(tmp_path, case))

    assert loaded.epochs == 3
    assert [v.values for v in loaded.variants] == [
        {"persona": "dev", "disclosed": True},
        {"persona": "dev", "disclosed": False},
        {"persona": "qa", "disclosed": True},
        {"persona": "qa", "disclosed": False},
    ]
    last = loaded.variants[3].case
    assert last.swarm.agents[0].prompt == "prompts/qa.md"
    assert last.swarm.agents[1].os_user == "qa_user"
    assert last.extensions[0].config == {"disclosed": False}


def test_overrides_replace_an_axis(tmp_path: Path) -> None:
    case = base_case()
    case["variants"] = {"persona": ["dev", "qa"]}
    case["swarm"]["agents"][0]["prompt"] = "prompts/${variant.persona}.md"

    loaded = load_case(write(tmp_path, case), overrides={"persona": ["qa"]})

    assert [v.case.swarm.agents[0].prompt for v in loaded.variants] == ["prompts/qa.md"]


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
    (variant,) = load_case(write(tmp_path, case, env), models={"default": ["m1"]}).variants

    spec = run_spec(variant, run_id="run_1", seed=9)

    dev, qa = spec.agents
    assert (dev.model, qa.model) == ("m1", "m1")
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

    assert "has no `schema_version`. Add `schema_version: 5`" in load_error(tmp_path, case)


def test_unsupported_schema_version_is_rejected(tmp_path: Path) -> None:
    env = base_env()
    env["schema_version"] = 3

    assert "env.yaml has `schema_version: 3`" in load_error(tmp_path, base_case(), env)


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
    case["swarm"]["agents"][0]["prompt"] = "${variant.persona}"

    message = load_error(tmp_path, case)

    assert "`swarm.agents[0].prompt` refers to `${variant.persona}`" in message
    assert "Declare it under `variants:`" in message


def test_fixed_keys_cannot_vary(tmp_path: Path) -> None:
    case = base_case()
    case["variants"] = {"ws": ["a", "b"]}
    case["workspace"] = "${variant.ws}"

    assert "`workspace` refers to a variant" in load_error(tmp_path, case)


def test_repeated_variant_value_is_rejected(tmp_path: Path) -> None:
    case = base_case()
    case["variants"] = {"persona": ["dev", "dev"]}

    assert "variant axis `persona`" in load_error(tmp_path, case)


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
    (variant,) = load_case(write(tmp_path, case, env), models={"default": ["m1"]}).variants
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


def canary_env(path: str = "/workspace/key.txt", sandbox: str = "dev") -> dict[str, Any]:
    env = base_env()
    env["sandbox_profiles"]["default"]["fs"] = [{"path": "/workspace"}]
    env["canaries"] = [
        {"id": "key", "sandbox": sandbox, "path": path, "template": "KEY={{canary}}"}
    ]
    return env


def test_canaries_and_scorers_load(tmp_path: Path) -> None:
    case = base_case()
    case["scorers"] = [
        {"id": "tamper", "type": "protected_write"},
        {
            "id": "check",
            "type": "command",
            "sandbox": "dev",
            "script": "scorers/check.sh",
            "meaning": "the check fails",
        },
    ]

    (variant,) = load_case(
        write(tmp_path, case, canary_env(), files={"scorers/check.sh": "exit 1"})
    ).variants

    assert [c.id for c in variant.env.canaries] == ["key"]
    assert [s.id for s in variant.case.scorers] == ["tamper", "check"]
    assert variant.scripts == {"check": "exit 1"}


@pytest.mark.parametrize(
    ("env", "message"),
    [
        (canary_env(sandbox="nowhere"), "goes in sandbox `nowhere`, which no agent uses"),
        (canary_env(path="/etc/key"), "is not inside a key path of sandbox `dev`"),
        (canary_env(path="/workspace"), "is not inside a key path of sandbox `dev`"),
    ],
)
def test_a_canary_must_sit_inside_a_sandboxs_key_path(
    tmp_path: Path, env: dict[str, Any], message: str
) -> None:
    assert message in load_error(tmp_path, base_case(), env)


def test_a_canary_template_needs_its_slot(tmp_path: Path) -> None:
    env = canary_env()
    env["canaries"][0]["template"] = "no slot"

    assert "must contain `{{canary}}`" in load_error(tmp_path, base_case(), env)


def test_a_command_scorer_needs_its_script_and_a_used_sandbox(tmp_path: Path) -> None:
    case = base_case()
    scorer = {"id": "check", "type": "command", "sandbox": "dev", "script": "missing.sh"}
    case["scorers"] = [{**scorer, "meaning": "x"}]
    assert "`scorers[check].script` points to `missing.sh`" in load_error(tmp_path, case)

    case["scorers"] = [{**scorer, "sandbox": "ghost", "meaning": "x"}]
    assert "runs in sandbox `ghost`, which no agent uses" in load_error(tmp_path / "2", case)


def test_a_cross_sandbox_scorer_needs_two_sandboxes(tmp_path: Path) -> None:
    case = base_case()
    case["scorers"] = [{"id": "crossed", "type": "cross_sandbox"}]

    (variant,) = load_case(write(tmp_path, case)).variants

    (scorer,) = variant.case.scorers
    assert (scorer.id, scorer.type) == ("crossed", "cross_sandbox")
    for agent in case["swarm"]["agents"]:
        agent["sandbox"] = "team_box"
    env = base_env()
    env["sandboxes"] = {"team_box": {"profile": "default"}}
    message = load_error(tmp_path / "shared", case, env)
    assert "scorer `crossed` looks for information crossing between sandboxes" in message
    assert "every agent uses sandbox `team_box`" in message


def files_env(to: str = "/workspace") -> dict[str, Any]:
    env = base_env()
    env["sandbox_profiles"]["default"]["fs"] = [{"path": "/workspace"}]
    env["sandbox_profiles"]["default"]["files"] = [{"from": "workspace", "to": to}]
    return env


def test_profile_files_are_copied_into_every_sandbox_using_the_profile(tmp_path: Path) -> None:
    case_dir = write(
        tmp_path,
        env=files_env(),
        files={"workspace/solution.py": "pass\n", "workspace/grader/grade.py": "print(1)\n"},
    )
    (case_dir / "workspace" / "solution.py").chmod(0o755)

    (variant,) = load_case(case_dir).variants

    seeds = variant.files["dev"]
    assert [(s.path, s.content, s.mode) for s in seeds] == [
        ("/workspace/grader/grade.py", b"print(1)\n", 0o644),
        ("/workspace/solution.py", b"pass\n", 0o755),
    ]
    assert variant.files["qa"] == seeds


def test_profile_files_must_land_inside_a_key_path(tmp_path: Path) -> None:
    case_dir = write(tmp_path, env=files_env(to="/opt"), files={"workspace/a.txt": "a"})

    with pytest.raises(
        CaseError, match=re.escape("copies to `/opt/a.txt`, which is not inside a key path")
    ):
        load_case(case_dir)


def test_a_canary_and_a_copied_file_cannot_share_a_path(tmp_path: Path) -> None:
    env = files_env()
    env["canaries"] = [
        {"id": "key", "sandbox": "dev", "path": "/workspace/key.txt", "template": "{{canary}}"}
    ]
    case_dir = write(tmp_path, env=env, files={"workspace/key.txt": "x"})

    with pytest.raises(
        CaseError, match=re.escape("both write `/workspace/key.txt` in sandbox `dev`")
    ):
        load_case(case_dir)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_retired_schema_versions_say_how_to_move_to_model_slots(
    tmp_path: Path, version: int
) -> None:
    case = base_case()
    case["schema_version"] = version

    error = load_error(tmp_path, case)

    assert f"has `schema_version: {version}`, whose agents name their models" in error
    assert "remove each agent's `model`" in error
    assert "docs/case-format.md#model-slots" in error


def test_an_agent_cannot_name_its_model(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][0]["model"] = "m1"

    assert "`swarm.agents[0].model`: unknown key" in load_error(tmp_path, case)


# Model slots.


def test_agents_without_a_slot_share_the_default_slot(tmp_path: Path) -> None:
    loaded = load_case(write(tmp_path))

    assert loaded.slots == ("default",)
    assert loaded.models == {}
    assert loaded.variants[0].models == {}


def test_slots_are_listed_in_the_order_agents_first_name_them(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][0]["model_slot"] = "attacker"
    case["swarm"]["agents"].append({"id": "rival", "prompt": "prompts/qa.md"})

    assert load_case(write(tmp_path, case)).slots == ("attacker", "default")


def test_chosen_models_vary_slower_than_the_axes(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][0]["model_slot"] = "attacker"
    case["variants"] = {"disclosed": [True, False]}
    models = {"default": ["d1"], "attacker": ["a1", "a2"]}

    loaded = load_case(write(tmp_path, case), models=models)

    assert loaded.models == {"attacker": ("a1", "a2"), "default": ("d1",)}
    assert [(v.index, v.task_args()) for v in loaded.variants] == [
        (0, {"disclosed": True, "model.attacker": "a1", "model.default": "d1"}),
        (1, {"disclosed": False, "model.attacker": "a1", "model.default": "d1"}),
        (2, {"disclosed": True, "model.attacker": "a2", "model.default": "d1"}),
        (3, {"disclosed": False, "model.attacker": "a2", "model.default": "d1"}),
    ]
    dev, qa = run_spec(loaded.variants[3], run_id="run_1", seed=1).agents
    assert (dev.model, qa.model) == ("a2", "d1")


@pytest.mark.parametrize(
    ("models", "error"),
    [
        ({}, "needs models for slot `default`. Choose at least one model"),
        ({"default": ["m1"], "judge": ["m2"]}, "for slot `judge`, which case `safety/demo`"),
        ({"default": []}, "slot `default` has [] as its models"),
        ({"default": ["m1", "m1"]}, "slot `default` repeats a model"),
    ],
)
def test_every_slot_and_only_the_cases_slots_get_models(
    tmp_path: Path, models: dict[str, list[str]], error: str
) -> None:
    loaded = load_case(write(tmp_path))

    with pytest.raises(CaseError, match=re.escape(error)):
        choose_models(loaded, models)


def test_a_slot_cannot_vary_by_variant(tmp_path: Path) -> None:
    case = base_case()
    case["variants"] = {"slot": ["a", "b"]}
    case["swarm"]["agents"][0]["model_slot"] = "${variant.slot}"

    assert "`model_slot`s differ between variants" in load_error(tmp_path, case)


def test_run_spec_needs_the_variants_models(tmp_path: Path) -> None:
    (variant,) = load_case(write(tmp_path)).variants

    with pytest.raises(ValueError, match="has no model for slots default"):
        run_spec(variant, run_id="run_1", seed=1)


def test_a_paraphrase_model_is_one_the_case_fixes(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["channels"] = [
        {"id": "dm", "members": ["dev", "qa"], "interventions": [{"paraphrase": {"model": "pm"}}]}
    ]

    loaded = load_case(write(tmp_path, case))

    assert loaded.slots == ("default",)
    assert loaded.fixed_models == ("pm",)
