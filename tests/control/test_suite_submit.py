"""A suite submitted through the Control API: one submission per case under one suite label,
which `ListRuns` and reports filter on."""

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import grpc
import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.analysis import load_summaries, report
from swarmeval.control.bundles import pack
from swarmeval.control.live import EventListener
from swarmeval.control.queue import Queue
from swarmeval.control.service import ControlService
from swarmeval.control.suite import submit
from swarmeval.core import load_suite
from swarmeval.events import ObjectStore
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import (
    ControlServiceStub,
    add_ControlServiceServicer_to_server,
)
from tests.core.test_loader import write
from tests.core.test_suite import model_case, write_suite
from tests.queueing import enqueue, start

if TYPE_CHECKING:
    from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import ControlServiceAsyncStub

pytestmark = pytest.mark.docker


@pytest.fixture
async def control(
    postgres_url: str, engine: AsyncEngine, object_store: ObjectStore
) -> AsyncIterator["ControlServiceAsyncStub"]:
    listener = EventListener(postgres_url)
    await listener.start()
    server = grpc.aio.server()
    add_ControlServiceServicer_to_server(
        ControlService(queue=Queue(engine), engine=engine, store=object_store, listener=listener),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        yield ControlServiceStub(channel)
    await server.stop(None)
    await listener.close()


async def test_a_suite_is_one_submission_per_case_under_one_label(
    control: "ControlServiceAsyncStub", object_store: ObjectStore, tmp_path: Path
) -> None:
    write(tmp_path / "a", model_case())
    other = model_case()
    other["id"] = "other"
    write(tmp_path / "b", other)
    path = write_suite(
        tmp_path,
        {
            "schema_version": 1,
            "id": "core",
            "models": ["m1", "m2"],
            "epochs": 2,
            "cases": [
                {"path": "../a/case"},
                {"path": "../b/case", "variants": {"framing": ["b"]}, "epochs": 1},
            ],
        },
    )

    label, submitted = await submit(load_suite(path), control)

    assert re.fullmatch(r"core\.[0-9a-f]{8}", label)
    assert [(s.case_id, s.runs) for s in submitted] == [("demo", 8), ("other", 2)]
    listed = (await control.ListRuns(pb.ListRunsRequest(suite=label))).runs
    assert len(listed) == 10
    assert {r.suite for r in listed} == {label}
    assert {r.submission_id for r in listed} == {s.submission_id for s in submitted}
    models = {r.task_args.fields["model"].string_value for r in listed if r.case_id == "other"}
    assert models == {"m1", "m2"}

    for run in listed:
        await control.CancelRun(pb.CancelRunRequest(run_id=run.run_id))
    result = report(load_summaries(object_store), suites=[label])
    assert sum(u.runs for u in result.unscored if u.status == "cancelled") == 10
    assert sum(c.requested for c in result.coverage) == 10


async def test_a_suite_label_outside_the_alphabet_is_refused(
    control: "ControlServiceAsyncStub", tmp_path: Path
) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as info:
        await control.SubmitRuns(
            pb.SubmitRunsRequest(case_bundle=pack(write(tmp_path)), suite="Core Suite")
        )

    assert info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert "Core Suite" in str(info.value.details())


async def test_a_rerun_keeps_its_suite_label(engine: AsyncEngine, submission: str) -> None:
    queue = Queue(engine)
    (run_id,) = await enqueue(queue, submission, suite="core.0000aaaa")
    owner_epoch = await start(engine, run_id, "w_suite")

    rerun = await queue.finish(run_id, owner_epoch, "interrupted")

    assert rerun is not None
    assert (await queue.get(rerun)).suite == "core.0000aaaa"


async def _refused(control: "ControlServiceAsyncStub", request: pb.SubmitSuiteRequest) -> str:
    with pytest.raises(grpc.aio.AioRpcError) as info:
        await control.SubmitSuite(request)
    assert info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    return str(info.value.details())


async def test_a_suite_with_a_broken_case_queues_nothing(
    control: "ControlServiceAsyncStub", tmp_path: Path
) -> None:
    write(tmp_path / "a", model_case())
    suite = {
        "schema_version": 1,
        "id": "broken",
        "cases": [{"path": "../a/case"}, {"path": "../a/case", "variants": {"nope": [1]}}],
    }
    bundle = pack(tmp_path / "a" / "case")
    before = len((await control.ListRuns(pb.ListRunsRequest(case_id="demo", limit=1000))).runs)

    details = await _refused(
        control,
        pb.SubmitSuiteRequest(suite_yaml=yaml.safe_dump(suite), case_bundles={"../a/case": bundle}),
    )

    assert "`cases[1]` (../a/case)" in details and "nope" in details
    after = len((await control.ListRuns(pb.ListRunsRequest(case_id="demo", limit=1000))).runs)
    assert after == before


async def test_a_suite_needs_exactly_the_bundles_it_names(
    control: "ControlServiceAsyncStub", tmp_path: Path
) -> None:
    write(tmp_path / "a", model_case())
    bundle = pack(tmp_path / "a" / "case")
    suite = yaml.safe_dump({"schema_version": 1, "id": "s", "cases": [{"path": "../a/case"}]})

    missing = await _refused(control, pb.SubmitSuiteRequest(suite_yaml=suite))
    extra = await _refused(
        control,
        pb.SubmitSuiteRequest(
            suite_yaml=suite, case_bundles={"../a/case": bundle, "../b/case": bundle}
        ),
    )
    not_tar = await _refused(
        control, pb.SubmitSuiteRequest(suite_yaml=suite, case_bundles={"../a/case": b"x"})
    )

    assert "`../a/case`" in missing and "no bundle" in missing
    assert "`../b/case`" in extra
    assert "`../a/case`" in not_tar and "tar" in not_tar


async def test_one_case_twice_is_two_submissions_from_one_bundle(
    control: "ControlServiceAsyncStub", tmp_path: Path
) -> None:
    write(tmp_path / "a", model_case())
    suite = {
        "schema_version": 1,
        "id": "twice",
        "epochs": 1,
        "cases": [
            {"path": "../a/case", "variants": {"framing": ["a"]}},
            {"path": "../a/case", "variants": {"framing": ["b"]}},
        ],
    }

    response = await control.SubmitSuite(
        pb.SubmitSuiteRequest(
            suite_yaml=yaml.safe_dump(suite),
            case_bundles={"../a/case": pack(tmp_path / "a" / "case")},
            actor="ada",
        )
    )

    first, second = response.submissions
    assert first.submission_id != second.submission_id
    runs = (await control.ListRuns(pb.ListRunsRequest(suite=response.suite))).runs
    assert sorted(r.task_args.fields["framing"].string_value for r in runs) == ["a", "b"]
    assert {r.submitted_by for r in runs} == {"ada"}
    assert len({r.case_sha256 for r in runs}) == 1
