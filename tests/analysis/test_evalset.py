import asyncio
from pathlib import Path

import pytest
from inspect_ai.log import read_eval_log

from swarmeval.analysis.evalset import EvalSetError, describe, variant_runs, write_eval_set
from swarmeval.events import LeftOut, ObjectStore, export_key, write_eval
from tests.analysis.test_report import summary, table
from tests.events.test_variant import run_log


def test_runs_are_grouped_by_submission_case_revision_and_variant() -> None:
    summaries = table(
        {**summary("r2"), "epoch": 2},
        summary("r1"),
        {**summary("r3", status="interrupted"), "epoch": 3},
        summary("r4", variant=1),
        summary("r5", case_sha256="cd" * 32),
        summary("r6", submission="sub2"),
        summary("r7", variant=2, status="failed"),
    )

    groups = variant_runs(summaries, ["sub1"])

    assert [(g.case_sha256[:2], g.variant, g.done) for g in groups] == [
        ("ab", 0, ("r1", "r2")),
        ("ab", 1, ("r4",)),
        ("ab", 2, ()),
        ("cd", 0, ("r5",)),
    ]
    assert groups[0].left_out == (LeftOut(run_id="r3", epoch=3, status="interrupted"),)
    assert groups[0].filename == f"sub1_scorer_misbelief-{'ab' * 4}_v0.eval"
    assert len(variant_runs(summaries)) == 5


@pytest.mark.docker
async def test_the_job_writes_one_log_per_variant_from_the_bucket(
    object_store: ObjectStore, tmp_path: Path
) -> None:
    for epoch, tampered in ((1, 1), (2, 0)):
        log = await run_log(epoch, tampered=tampered, run_id=f"evalset_e{epoch}")
        path = tmp_path / f"e{epoch}.eval"
        write_eval(log, path)
        await asyncio.to_thread(
            object_store.put, export_key(f"evalset_e{epoch}"), path.read_bytes()
        )
    summaries = table(
        {**summary("evalset_e1"), "submission_id": "sub_es"},
        {**summary("evalset_e2"), "submission_id": "sub_es", "epoch": 2},
        {**summary("evalset_e3", status="failed"), "submission_id": "sub_es", "epoch": 3},
        {**summary("evalset_v1", variant=1, status="cancelled"), "submission_id": "sub_es"},
    )
    variants = variant_runs(summaries)

    written = await write_eval_set(object_store, variants, tmp_path / "out")

    assert [w.path is not None for w in written] == [True, False]
    path = written[0].path
    assert path is not None
    log = read_eval_log(path)
    assert log.samples is not None
    assert [s.epoch for s in log.samples] == [1, 2]
    assert log.results is not None
    assert log.eval.metadata is not None
    assert log.eval.metadata["swarmeval"]["left_out"] == [
        {"run_id": "evalset_e3", "epoch": 3, "status": "failed"}
    ]
    lines = describe(written).splitlines()
    assert "2 epochs; left out: evalset_e3 (epoch 3, failed)" in lines[0]
    assert "no `done` run, not written; left out: evalset_v1" in lines[1]

    again = await write_eval_set(object_store, variants, tmp_path / "out")
    assert [w.path for w in again] == [w.path for w in written]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [path.name]


@pytest.mark.docker
async def test_a_done_run_without_a_log_is_reported(
    object_store: ObjectStore, tmp_path: Path
) -> None:
    variants = variant_runs(table({**summary("evalset_missing"), "submission_id": "sub_miss"}))

    with pytest.raises(EvalSetError, match="evalset_missing is `done`"):
        await write_eval_set(object_store, variants, tmp_path)
