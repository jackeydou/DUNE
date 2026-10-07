"""Models chosen at submission (spec/2026-10-06-run-time-models): every slot gets models,
model-gateway must serve each, and the runs carry them."""

import asyncio
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

import grpc
import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.bundles import bundle_hash, bundle_key, pack
from swarmeval.control.cases import add_revision
from swarmeval.events import ObjectStore
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from tests.core.test_loader import base_case, write
from tests.models import StaticModels, chosen

if TYPE_CHECKING:
    from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import ControlServiceAsyncStub

pytestmark = pytest.mark.docker


def duel(workspace: str) -> dict[str, Any]:
    case = base_case()
    case["workspace"] = workspace
    case["swarm"]["agents"][0]["model_slot"] = "attacker"
    case["variants"] = {"framing": ["a", "b"]}
    return case


async def refused(call: Any) -> tuple[grpc.StatusCode, str]:
    with pytest.raises(grpc.aio.AioRpcError) as info:
        await call
    return info.value.code(), str(info.value.details())


@pytest.fixture
def workspace() -> str:
    return f"ws-{secrets.token_hex(4)}"


async def test_each_slot_is_a_matrix_dimension_and_runs_name_their_models(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    bundle = pack(write(tmp_path, duel(workspace)))
    models = {**chosen("m1", "m2", slot="attacker"), **chosen("m3")}

    submitted = await control.SubmitRuns(
        pb.SubmitRunsRequest(case_bundle=bundle, models=models, epochs=1)
    )

    runs = (await control.ListRuns(pb.ListRunsRequest(workspace=workspace))).runs
    assert len(submitted.run_ids) == len(runs) == 4
    combos = sorted(
        (
            r.variant,
            r.task_args.fields["model.attacker"].string_value,
            r.task_args.fields["model.default"].string_value,
            r.task_args.fields["framing"].string_value,
        )
        for r in runs
    )
    assert combos == [
        (0, "m1", "m3", "a"),
        (1, "m1", "m3", "b"),
        (2, "m2", "m3", "a"),
        (3, "m2", "m3", "b"),
    ]
    for run_id in submitted.run_ids:
        await control.CancelRun(pb.CancelRunRequest(run_id=run_id))


async def test_a_submission_names_models_for_exactly_the_cases_slots(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    bundle = pack(write(tmp_path, duel(workspace)))

    none = await refused(control.SubmitRuns(pb.SubmitRunsRequest(case_bundle=bundle)))
    extra = await refused(
        control.SubmitRuns(
            pb.SubmitRunsRequest(
                case_bundle=bundle,
                models={**chosen(slot="attacker"), **chosen(), **chosen(slot="judge")},
            )
        )
    )

    assert none[0] == grpc.StatusCode.INVALID_ARGUMENT
    assert "needs models for slots `attacker`, `default`" in none[1]
    assert extra[0] == grpc.StatusCode.INVALID_ARGUMENT
    assert "for slot `judge`, which case" in extra[1]
    listed = await control.ListCases(pb.ListCasesRequest(workspace=workspace))
    assert list(listed.cases) == []


async def test_a_model_the_gateway_does_not_serve_is_refused_before_anything_is_stored(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    bundle = pack(write(tmp_path, {**base_case(), "workspace": workspace}))

    code, details = await refused(
        control.SubmitRuns(pb.SubmitRunsRequest(case_bundle=bundle, models=chosen("m1", "gpt-x")))
    )

    assert code == grpc.StatusCode.INVALID_ARGUMENT
    assert "slot `default` is to run on model `gpt-x`" in details
    assert "It serves: m1, m2, m3." in details
    listed = await control.ListCases(pb.ListCasesRequest(workspace=workspace))
    assert list(listed.cases) == []


async def test_a_model_the_case_fixes_must_be_served_too(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    case: dict[str, Any] = {**base_case(), "workspace": workspace}
    case["swarm"]["channels"] = [
        {"id": "dm", "members": ["dev", "qa"], "interventions": [{"paraphrase": {"model": "pm"}}]}
    ]
    bundle = pack(write(tmp_path, case))

    code, details = await refused(
        control.SubmitRuns(pb.SubmitRunsRequest(case_bundle=bundle, models=chosen()))
    )

    assert code == grpc.StatusCode.INVALID_ARGUMENT
    assert "itself uses model `pm` (a `paraphrase` intervention's `model`)" in details


async def test_without_the_gateway_nothing_is_submitted(
    control: "ControlServiceAsyncStub", models: StaticModels, workspace: str, tmp_path: Path
) -> None:
    models.unavailable = True
    bundle = pack(write(tmp_path, {**base_case(), "workspace": workspace}))

    submit = await refused(
        control.SubmitRuns(pb.SubmitRunsRequest(case_bundle=bundle, models=chosen()))
    )
    listing = await refused(control.ListModels(pb.ListModelsRequest()))

    assert submit[0] == listing[0] == grpc.StatusCode.UNAVAILABLE
    assert "did not list its models" in submit[1]
    listed = await control.ListCases(pb.ListCasesRequest(workspace=workspace))
    assert list(listed.cases) == []


async def test_list_models_is_what_the_gateway_serves(
    control: "ControlServiceAsyncStub",
) -> None:
    listed = await control.ListModels(pb.ListModelsRequest())

    assert list(listed.models) == ["m1", "m2", "m3"]


async def test_a_suite_is_checked_against_the_gateway(
    control: "ControlServiceAsyncStub", workspace: str, tmp_path: Path
) -> None:
    write(tmp_path / "a", {**base_case(), "workspace": workspace})
    suite = {
        "schema_version": 2,
        "id": "s",
        "models": ["m1", "nope"],
        "cases": [{"path": "../a/case"}],
    }

    code, details = await refused(
        control.SubmitSuite(
            pb.SubmitSuiteRequest(
                suite_yaml=yaml.safe_dump(suite),
                case_bundles={"../a/case": pack(tmp_path / "a" / "case")},
            )
        )
    )

    assert code == grpc.StatusCode.INVALID_ARGUMENT
    assert "model `nope`" in details


async def test_a_revision_names_its_slots_or_why_it_cannot_run(
    control: "ControlServiceAsyncStub",
    engine: AsyncEngine,
    object_store: ObjectStore,
    workspace: str,
    tmp_path: Path,
) -> None:
    await control.PushCase(pb.PushCaseRequest(case_bundle=pack(write(tmp_path, duel(workspace)))))
    retired: dict[str, Any] = {
        **base_case(),
        "workspace": workspace,
        "id": "old",
        "schema_version": 3,
    }
    for agent in retired["swarm"]["agents"]:
        agent["model"] = "m1"
    # A revision stored before version 4, as the library still holds it.
    old = pack(write(tmp_path / "old", retired))
    await asyncio.to_thread(object_store.put, bundle_key(bundle_hash(old)), old)
    async with engine.begin() as conn:
        await add_revision(
            conn,
            workspace=workspace,
            case_id="old",
            sha256=bundle_hash(old),
            actor=None,
            note=None,
        )

    current = await control.GetCaseRevision(
        pb.GetCaseRevisionRequest(workspace=workspace, case_id="demo")
    )
    stored = await control.GetCaseRevision(
        pb.GetCaseRevisionRequest(workspace=workspace, case_id="old")
    )
    run = await refused(
        control.SubmitRuns(
            pb.SubmitRunsRequest(
                case=pb.CaseRevisionRef(workspace=workspace, case_id="old"), models=chosen()
            )
        )
    )

    assert (list(current.model_slots), current.load_error) == (["attacker", "default"], "")
    assert list(stored.model_slots) == []
    assert "`schema_version: 3`, whose agents name their models" in stored.load_error
    assert {f.path for f in stored.files} >= {"case.yaml", "env.yaml"}
    assert run[0] == grpc.StatusCode.INVALID_ARGUMENT
    assert "whose agents name their models" in run[1]
