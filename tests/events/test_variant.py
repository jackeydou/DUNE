"""Per-variant logs: every epoch a sample, results computed by Inspect, and readable by it."""

from dataclasses import replace
from pathlib import Path

import pytest
from inspect_ai.log import EvalLog, read_eval_log

from swarmeval.events import (
    LeftOut,
    VariantLogError,
    assemble,
    read_eval,
    variant_log,
    write_eval,
)
from swarmeval.runtime.records import EventDraft, ScoreRecord, Transaction
from tests.events.test_export import HEADER, stored, two_agent_run


async def run_log(
    epoch: int, *, tampered: int, read_key: int = 0, run_id: str | None = None
) -> EvalLog:
    """One epoch's per-run log, as the worker exports it."""
    fake = await two_agent_run()
    verdicts = [
        ScoreRecord(scorer="tampered", value=1 if tampered else 0, meaning="m", explanation="e"),
        ScoreRecord(scorer="read_key", value=1 if read_key else 0, meaning="m", explanation="e"),
    ]
    await fake.commit(Transaction(events=[EventDraft(record=v) for v in verdicts]))
    log = assemble(replace(HEADER, epoch=epoch), stored(fake))
    # The fake run's chain is rooted at HEADER's run id; give each epoch its own afterwards.
    assert log.samples is not None
    log.eval.run_id = log.samples[0].uuid = run_id or f"run_e{epoch}"
    return log


def metrics(log: EvalLog, scorer: str, reducer: str | None) -> dict[str, float]:
    assert log.results is not None
    (score,) = [s for s in log.results.scores if s.name == scorer and s.reducer == reducer]
    return {name: m.value for name, m in score.metrics.items()}


async def test_epochs_become_samples_of_one_log_that_inspect_reads_back(tmp_path: Path) -> None:
    runs = [
        await run_log(3, tampered=1),
        await run_log(1, tampered=1),
        await run_log(2, tampered=0),
    ]
    left_out = [LeftOut(run_id="run_e4", epoch=4, status="interrupted")]

    log = variant_log(runs, submission_id="sub1", case_sha256="ab" * 32, left_out=left_out)
    path = tmp_path / "variant.eval"
    write_eval(log, path)
    back = read_eval_log(path)

    assert back.samples is not None
    assert [(s.id, s.epoch, s.uuid) for s in back.samples] == [
        ("demo", 1, "run_e1"),
        ("demo", 2, "run_e2"),
        ("demo", 3, "run_e3"),
    ]
    assert back.eval.config.epochs == 3
    assert back.eval.config.epochs_reducer == ["mean"]
    assert back.eval.run_id == "sub1"
    assert back.eval.metadata is not None
    ours = back.eval.metadata["swarmeval"]
    assert ours["runs"] == {"1": "run_e1", "2": "run_e2", "3": "run_e3"}
    assert ours["left_out"] == [{"run_id": "run_e4", "epoch": 4, "status": "interrupted"}]
    assert ours["case_sha256"] == "ab" * 32
    assert metrics(back, "tampered", "mean") == {"mean": pytest.approx(2 / 3)}
    epoch_level = metrics(back, "tampered", None)
    assert epoch_level["epoch_stderr"] == pytest.approx(1 / 3)
    assert epoch_level["lower"] == pytest.approx(0.2077, abs=1e-4)
    assert epoch_level["upper"] == pytest.approx(0.9385, abs=1e-4)
    assert metrics(back, "read_key", "mean") == {"mean": 0.0}
    assert back.reductions is not None


async def test_a_log_read_from_bytes_assembles_like_the_original(tmp_path: Path) -> None:
    original = await run_log(1, tampered=1)
    path = tmp_path / "run.eval"
    write_eval(original, path)

    log = variant_log([read_eval(path.read_bytes())], submission_id="s", case_sha256="cd" * 32)

    assert metrics(log, "tampered", "mean") == {"mean": 1.0}


async def test_each_reducer_adds_a_reduced_score_per_scorer() -> None:
    runs = [await run_log(e, tampered=t) for e, t in ((1, 1), (2, 0), (3, 1))]

    log = variant_log(
        runs, submission_id="s", case_sha256="ab" * 32, reducers=["mean", "at_least_3"]
    )

    assert metrics(log, "tampered", "at_least_3") == {"mean": 0.0}
    assert metrics(log, "tampered", "mean") == {"mean": pytest.approx(2 / 3)}


async def test_an_unknown_reducer_is_refused() -> None:
    with pytest.raises(VariantLogError, match="reducers sometimes"):
        variant_log(
            [await run_log(1, tampered=1)],
            submission_id="s",
            case_sha256="ab" * 32,
            reducers=["sometimes"],
        )


async def test_runs_of_different_variants_are_refused() -> None:
    other = await run_log(2, tampered=0)
    other.eval.task_args = {"model": "other-model"}

    with pytest.raises(VariantLogError, match="run_e1 and run_e2 are not the same variant"):
        variant_log([await run_log(1, tampered=1), other], submission_id="s", case_sha256="ab" * 32)


async def test_two_runs_of_one_epoch_are_refused() -> None:
    first, second = await run_log(1, tampered=1), await run_log(1, tampered=0)
    second.eval.run_id = "run_again"

    with pytest.raises(VariantLogError, match="are all epoch 1"):
        variant_log([first, second], submission_id="s", case_sha256="ab" * 32)
