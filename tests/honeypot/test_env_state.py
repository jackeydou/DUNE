from swarmeval.honeypot.env_state import setup
from swarmeval.runtime.extensions import ExtensionUse
from swarmeval.runtime.records import ExecResult, ExtensionEmitRecord, SandboxExecRecord
from tests.runtime.fakes import FakeSandbox, agent, harness, reply


async def test_snapshots_are_taken_at_start_after_turns_and_at_end() -> None:
    use = ExtensionUse.model_validate(
        {
            "use": "swarmeval.env_state",
            "config": {
                "snapshots": [{"id": "git", "sandbox": "box_a", "run": "git status --short"}],
                "every_turn": True,
            },
        }
    )
    sandbox = FakeSandbox(handler=lambda _: ExecResult(exit_code=0, stdout=" M a.py", stderr=""))
    h = harness((agent(),), {"a": [reply("done")]}, extensions=[(setup, use)], sandbox=sandbox)

    await h.loop.run()

    states = [
        e.record.data
        for e in h.store.events
        if isinstance(e.record, ExtensionEmitRecord) and e.record.name == "env.state"
    ]
    assert [s["when"] for s in states if isinstance(s, dict)] == [
        "run_start",
        "after_turn",
        "run_end",
    ]
    assert all(isinstance(s, dict) and s["output"] == " M a.py" for s in states)
    assert len([e for e in h.store.events if isinstance(e.record, SandboxExecRecord)]) == 3
    assert sandbox.call_ids == [
        "ext:swarmeval.env_state:1",
        "ext:swarmeval.env_state:2",
        "ext:swarmeval.env_state:3",
    ]
