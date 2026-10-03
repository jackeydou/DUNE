"""The isolation self-check's orchestration, with sandboxd scripted. The scripts themselves run
in real sandboxes in `test_probes_live.py`."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from swarmeval.core import load_case
from swarmeval.honeypot import place_sandboxes
from swarmeval.runtime.records import Exec, ExecResult, IsolationProbeRecord, LifecycleRecord
from swarmeval.runtime.writer import RunWriter
from swarmeval.worker.probes import IsolationError, ProbeSandbox, check_isolation
from swarmeval.worker.run import probe_targets
from tests.core.test_loader import base_case, base_env, write
from tests.runtime.fakes import FakeStore

IDS = ("a", "b")


def target(sandbox_id: str) -> ProbeSandbox:
    return ProbeSandbox(
        sandbox_id=sandbox_id,
        names=(f"host-{sandbox_id}", sandbox_id),
        key_paths=("/workspace", "/workspace/tests"),
        plant_dirs=("/workspace",),
    )


@dataclass
class ScriptedSandboxes:
    """Plays the probe scripts: every sandbox plants, and every check reports `isolated` unless
    told otherwise."""

    ids: tuple[str, ...] = IDS
    leaks: set[tuple[str, str, str | None]] = field(
        default_factory=set[tuple[str, str, str | None]]
    )
    """(sandbox, probe, peer) the check reports as leaked."""
    silent: set[str] = field(default_factory=set[str])
    """Sandboxes whose check prints nothing and exits 127."""
    unplanted: set[str] = field(default_factory=set[str])
    survivors: set[str] = field(default_factory=set[str])
    calls: list[tuple[str, str | None, Exec, str]] = field(
        default_factory=list[tuple[str, str | None, Exec, str]]
    )

    async def exec(
        self, sandbox_id: str, os_user: str | None, command: Exec, *, call_id: str
    ) -> ExecResult:
        self.calls.append((sandbox_id, os_user, command, call_id))
        out = ""
        code = 0
        match call_id:
            case "probe:plant":
                planted = "" if sandbox_id in self.unplanted else "planted /workspace\n"
                out = f"{planted}proc 4{len(self.calls)}\n"
            case "probe:check" if sandbox_id in self.silent:
                code = 127
            case "probe:check":
                expected = [("interfaces", None), ("connect", None)]
                for peer in self.ids:
                    if peer != sandbox_id:
                        expected += [("proc", peer), ("shared_path", peer), ("dns", peer)]
                for probe, peer in expected:
                    leaked = (sandbox_id, probe, peer) in self.leaks
                    outcome = "leaked" if leaked else "isolated"
                    out += f"R {probe} {peer or '-'} {outcome} scripted {probe}\n"
            case "probe:clean" if sandbox_id in self.survivors:
                out = "alive 41 42\n"
            case _:
                pass
        return ExecResult(exit_code=code, stdout=out, stderr="sh: not found" if code else "")

    def commands(self, call_id: str) -> dict[str, Exec]:
        return {s: c for s, _, c, i in self.calls if i == call_id}


def probe_events(store: FakeStore) -> list[IsolationProbeRecord]:
    return [e.record for e in store.events if isinstance(e.record, IsolationProbeRecord)]


async def test_isolated_sandboxes_pass_and_every_step_is_recorded() -> None:
    sandboxes, store = ScriptedSandboxes(), FakeStore()

    findings = await check_isolation([target(s) for s in IDS], sandboxes, RunWriter(store))

    assert {s: [(f.probe, f.peer, f.outcome) for f in fs] for s, fs in findings.items()} == {
        "a": [
            ("interfaces", None, "isolated"),
            ("connect", None, "isolated"),
            ("proc", "b", "isolated"),
            ("shared_path", "b", "isolated"),
            ("dns", "b", "isolated"),
        ],
        "b": [
            ("interfaces", None, "isolated"),
            ("connect", None, "isolated"),
            ("proc", "a", "isolated"),
            ("shared_path", "a", "isolated"),
            ("dns", "a", "isolated"),
        ],
    }
    events = probe_events(store)
    assert [(e.sandbox_id, e.step) for e in events] == [
        (s, step) for step in ("plant", "check", "clean") for s in IDS
    ]
    assert [e.findings for e in events if e.step == "check"] == [findings["a"], findings["b"]]
    assert {(user, call_id) for _, user, _, call_id in sandboxes.calls} == {
        ("0", "probe:plant"),
        ("0", "probe:check"),
        ("0", "probe:clean"),
    }
    plant = sandboxes.commands("probe:plant")["a"]
    assert plant.argv[5:] == ("/workspace", "/dev/shm")
    clean = sandboxes.commands("probe:clean")["a"]
    assert clean.argv[4:] == (plant.argv[4], "41", "/workspace")
    assert not any(isinstance(e.record, LifecycleRecord) for e in store.events)


async def test_no_check_command_line_carries_a_whole_marker() -> None:
    sandboxes = ScriptedSandboxes()

    await check_isolation([target(s) for s in IDS], sandboxes, RunWriter(FakeStore()))

    markers = [c.argv[4] for c in sandboxes.commands("probe:plant").values()]
    assert all(m.startswith(".isolation-probe-") for m in markers)
    for check in sandboxes.commands("probe:check").values():
        script = " ".join(check.argv)
        assert not any(m in script for m in markers), "the /proc scan would find itself"
        assert "host-b" in script or "host-a" in script


async def test_a_probe_that_gets_through_fails_the_run_after_cleaning_up() -> None:
    sandboxes = ScriptedSandboxes(leaks={("b", "shared_path", "a"), ("a", "interfaces", None)})
    store = FakeStore()

    with pytest.raises(IsolationError) as info:
        await check_isolation([target(s) for s in IDS], sandboxes, RunWriter(store))

    message = str(info.value)
    assert "sandbox `a` network: interfaces got through (scripted interfaces)" in message
    assert "sandbox `b` and sandbox `a`: shared_path got through (scripted shared_path)" in message
    assert [e.step for e in probe_events(store)][-2:] == ["clean", "clean"]
    failed = store.events[-1].record
    assert isinstance(failed, LifecycleRecord)
    assert (failed.status, failed.reason, failed.error) == (
        "failed",
        "isolation self-check",
        message,
    )


async def test_a_check_that_prints_nothing_is_unverified_not_a_failure() -> None:
    sandboxes = ScriptedSandboxes(silent={"b"})

    findings = await check_isolation([target(s) for s in IDS], sandboxes, RunWriter(FakeStore()))

    assert {f.outcome for f in findings["b"]} == {"unverified"}
    assert findings["b"][0].detail == "the check printed no result; it exited 127: sh: not found"
    assert {f.outcome for f in findings["a"]} == {"isolated"}


async def test_a_marker_that_could_not_be_planted_makes_the_path_probe_unverified() -> None:
    sandboxes = ScriptedSandboxes(unplanted={"b"})

    await check_isolation([target(s) for s in IDS], sandboxes, RunWriter(FakeStore()))

    checks = sandboxes.commands("probe:check")
    assert "say shared_path b unverified 'no directory there took a marker'" in checks["a"].argv[2]
    assert "say shared_path a unverified 'no directory here took a marker'" in checks["b"].argv[2]


async def test_a_probe_process_that_survives_cleanup_fails_the_run() -> None:
    sandboxes = ScriptedSandboxes(survivors={"a"})

    with pytest.raises(IsolationError) as info:
        await check_isolation([target(s) for s in IDS], sandboxes, RunWriter(FakeStore()))

    assert "sandbox `a`: probe processes 41 42 survived cleanup" in str(info.value)


def test_probe_targets_follow_the_case_topology(tmp_path: Path) -> None:
    case = base_case()
    case["swarm"]["agents"][1]["sandbox"] = "team_box"
    env = base_env()
    env["sandbox_profiles"]["default"]["fs"] = [
        {"path": "/workspace"},
        {"path": "/workspace/tests", "protected": True},
        {"path": "/data", "mode": "ro"},
    ]
    env["sandboxes"] = {"team_box": {"profile": "default"}}
    (variant,) = load_case(write(tmp_path, case, env)).variants
    boxes = place_sandboxes(
        {p.id: p.agents for p in variant.sandboxes.values()},
        {p.id: [] for p in variant.sandboxes.values()},
    )

    targets = probe_targets(variant, boxes)

    assert [(t.sandbox_id, t.names[1], t.key_paths, t.plant_dirs) for t in targets] == [
        ("dev", "dev", ("/workspace", "/workspace/tests", "/data"), ("/workspace",)),
        ("team_box", "team_box", ("/workspace", "/workspace/tests", "/data"), ("/workspace",)),
    ]
    assert [t.names[0] for t in targets] == [b.hostname for b in boxes]


async def test_one_sandbox_checks_only_its_way_out() -> None:
    sandboxes = ScriptedSandboxes(ids=("solo",))

    findings = await check_isolation([target("solo")], sandboxes, RunWriter(FakeStore()))

    assert [(f.probe, f.peer) for f in findings["solo"]] == [
        ("interfaces", None),
        ("connect", None),
    ]
