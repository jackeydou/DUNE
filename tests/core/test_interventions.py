"""Channel interventions in `case.yaml`: the `interventions:` shorthand, checks on the built-in
`swarmeval.bus.*` configs, and list-valued variant axes that switch them on and off."""

import logging
from pathlib import Path
from typing import Any

import pytest

from swarmeval.core import load_case
from swarmeval.runtime.extensions import ExtensionUse, load_extensions
from swarmeval.runtime.tools import BUILTIN_TOOL_NAMES
from tests.core.test_loader import base_case, load_error, write


def chat_case(**channel: Any) -> dict[str, Any]:
    case = base_case()
    case["swarm"]["channels"] = [{"id": "dm", "members": ["dev", "qa"], **channel}]
    return case


def test_the_shorthand_expands_to_one_builtin_per_entry_after_extensions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    case = chat_case(
        interventions=[
            "log",
            {"paraphrase": {"model": "m2"}},
            {"delay": {"turns": [1, 3]}},
            {"inject": {"at_turn": 2, "sender": "dev", "content": "deal?"}},
        ]
    )
    case["extensions"] = [{"use": "swarmeval.bus.drop", "config": {"channels": ["dm"], "p": 0.1}}]

    with caplog.at_level(logging.WARNING):
        loaded = load_case(write(tmp_path, case))

    (variant,) = loaded.variants
    assert variant.extensions == (
        ExtensionUse(use="swarmeval.bus.drop", config={"channels": ["dm"], "p": 0.1}),
        ExtensionUse.model_validate(
            {
                "use": "swarmeval.bus.paraphrase",
                "as": "dm.paraphrase",
                "config": {"model": "m2", "channels": ["dm"]},
            }
        ),
        ExtensionUse.model_validate(
            {
                "use": "swarmeval.bus.delay",
                "as": "dm.delay",
                "config": {"turns": [1, 3], "channels": ["dm"]},
            }
        ),
        ExtensionUse.model_validate(
            {
                "use": "swarmeval.bus.inject",
                "as": "dm.inject",
                "config": {"at_turn": 2, "sender": "dev", "content": "deal?", "channel": "dm"},
            }
        ),
    )
    assert len(loaded.warnings) == 1 and "`log` does nothing" in loaded.warnings[0]
    assert "swarm.channels[0].interventions[0]" in caplog.text
    # The worker loads exactly these.
    loaded_exts = load_extensions(variant.extensions, builtin_tools=BUILTIN_TOOL_NAMES)
    assert [e.instance_id for e in loaded_exts] == [
        "swarmeval.bus.drop",
        "dm.paraphrase",
        "dm.delay",
        "dm.inject",
    ]


def test_a_list_valued_axis_switches_an_intervention_per_variant(tmp_path: Path) -> None:
    case = chat_case()
    case["variants"] = {"paraphrased": [[], ["dm"]]}
    case["extensions"] = [
        {
            "use": "swarmeval.bus.paraphrase",
            "config": {"channels": "${variant.paraphrased}", "model": "m2"},
        }
    ]

    loaded = load_case(write(tmp_path, case))

    assert [v.values for v in loaded.variants] == [{"paraphrased": ()}, {"paraphrased": ("dm",)}]
    assert [v.extensions[0].config["channels"] for v in loaded.variants] == [[], ["dm"]]


def test_an_override_can_give_list_values(tmp_path: Path) -> None:
    case = chat_case()
    case["variants"] = {"paraphrased": [[]]}
    case["extensions"] = [
        {
            "use": "swarmeval.bus.paraphrase",
            "config": {"channels": "${variant.paraphrased}", "model": "m2"},
        }
    ]

    loaded = load_case(write(tmp_path, case), {"paraphrased": [("dm",), ()]})

    assert [v.values for v in loaded.variants] == [{"paraphrased": ("dm",)}, {"paraphrased": ()}]


def test_a_list_cannot_sit_inside_a_longer_string(tmp_path: Path) -> None:
    case = chat_case()
    case["variants"] = {"chans": [["dm"]]}
    case["description"] = "channels ${variant.chans}"

    assert "A list-valued axis can only be a field's whole value" in load_error(tmp_path, case)


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        ({"interventions": [{"drop": {"p": 1.5}}]}, "interventions[0]` (`dm.drop`): `p`"),
        (
            {"interventions": [{"drop": {"p": 0.5, "channels": ["dm"]}}]},
            "sets `channels`, but the intervention applies to channel `dm`",
        ),
        (
            {"interventions": [{"delay": {"seconds": 5}}]},
            "`seconds` needs the `async` turn policy",
        ),
        ({"interventions": ["paraphrase"]}, "(`dm.paraphrase`): `model`: Field required"),
        (
            {"interventions": [{"drop": {"p": 0.5}, "delay": {"turns": 1}}]},
            "names 2 interventions",
        ),
        (
            {"interventions": ["shuffle"]},
            "`swarm.channels[0].interventions`: unknown intervention `shuffle`. Known: log, drop",
        ),
    ],
)
def test_bad_intervention_configs_are_refused_at_load(
    tmp_path: Path, channel: dict[str, Any], expected: str
) -> None:
    assert expected in load_error(tmp_path, chat_case(**channel))


def test_a_builtin_naming_an_undeclared_channel_is_refused(tmp_path: Path) -> None:
    case = chat_case()
    case["extensions"] = [
        {
            "use": "swarmeval.bus.inject",
            "config": {"channel": "ops", "at_turn": 1, "sender": "dev", "content": "x"},
        }
    ]

    error = load_error(tmp_path, case)

    assert "`extensions[0]` (`swarmeval.bus.inject`) names channels ops" in error
    assert "Channels: dm." in error
