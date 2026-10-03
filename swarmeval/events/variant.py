"""One Inspect `.eval` per variant, assembled from the per-run logs of its epochs
(docs/event-log.md#export).

Each run's log holds one sample; the variant's log holds them all, one per epoch, with `results`
computed by Inspect itself (`recompute_metrics`) from the reducers and metrics declared in the
log header, so anyone holding the log can recompute them.

Inspect reduces epochs per sample before its metrics run, and a variant has one sample, so a
metric over reduced scores sees one value. The epoch-level spread therefore comes from metrics
declared over unreduced scores: `epoch_stderr` and `epoch_ci_wilson` treat each epoch as one
observation, as the trigger-rate report does.
"""

import io
from collections.abc import Sequence
from dataclasses import dataclass

from inspect_ai.log import (
    EvalConfig,
    EvalDataset,
    EvalLog,
    EvalSample,
    EvalSpec,
    read_eval_log,
    recompute_metrics,
)
from inspect_ai.scorer import Metric, ci_wilson, metric, stderr

DEFAULT_REDUCERS = ("mean",)
"""Runtime spec decision 11: the trigger rate is the mean over epochs."""


@metric(name="epoch_stderr", scores="unreduced")
def epoch_stderr() -> Metric:
    """Standard error of the mean over epochs, each epoch one observation."""
    return stderr()


@metric(name="epoch_ci_wilson", scores="unreduced")
def epoch_ci_wilson() -> Metric:
    """95% Wilson interval over epochs. Unlike mean ± 1.96·stderr, it does not collapse to a
    point when no epoch or every epoch triggered."""
    return ci_wilson()


_METRICS = ("mean", "swarmeval/epoch_stderr", "swarmeval/epoch_ci_wilson")
"""Registry names, as the log header records them: Inspect's `mean` over the reduced score,
and the two epoch-level metrics above, which Inspect registers under this package's name."""


class VariantLogError(ValueError):
    """The per-run logs do not form one variant, or a reducer is unknown."""


@dataclass(frozen=True)
class LeftOut:
    """A run of the variant that did not end `done`, so it is not a sample of the log."""

    run_id: str
    epoch: int
    status: str


def read_eval(data: bytes) -> EvalLog:
    """A `.eval` file's bytes, as Inspect reads them."""
    return read_eval_log(io.BytesIO(data), format="eval")


def variant_log(
    runs: Sequence[EvalLog],
    *,
    submission_id: str,
    case_sha256: str,
    reducers: Sequence[str] = DEFAULT_REDUCERS,
    left_out: Sequence[LeftOut] = (),
) -> EvalLog:
    """`runs` are the per-run logs of one variant's `done` epochs (`export_run`), any order.
    `reducers` are Inspect reducer names (`mean`, `median`, `max`, `at_least_<k>`, ...); each
    adds one reduced score per scorer to `results`."""
    if not runs:
        raise VariantLogError(
            f"submission {submission_id} case@{case_sha256[:8]}: no runs to assemble. A variant "
            "log needs at least one `done` epoch."
        )
    samples: list[EvalSample] = []
    for log in runs:
        if log.samples is None or len(log.samples) != 1:
            count = 0 if log.samples is None else len(log.samples)
            raise VariantLogError(
                f"run {log.eval.run_id}: its log has {count} samples, not 1. Read it with "
                "samples, from `runs/<run_id>/sample.eval`."
            )
        samples.append(log.samples[0])
    first = runs[0].eval
    _check_same_variant(runs)
    epochs = sorted(s.epoch for s in samples)
    repeated = sorted({e for e in epochs if epochs.count(e) > 1})
    if repeated:
        ids = [log.eval.run_id for log in runs if log.samples and log.samples[0].epoch in repeated]
        raise VariantLogError(
            f"runs {', '.join(ids)} are all epoch {repeated[0]} of one variant. Each epoch may "
            "be assembled once; leave the extra run out."
        )
    scorers = list(dict.fromkeys(name for s in samples for name in s.scores or {}))
    ours = dict((first.metadata or {}).get("swarmeval", {}))
    ours.update(
        submission_id=submission_id,
        case_sha256=case_sha256,
        runs={str(s.epoch): s.uuid for s in samples},
        left_out=[{"run_id": r.run_id, "epoch": r.epoch, "status": r.status} for r in left_out],
    )
    spec = EvalSpec.model_validate(
        {
            **first.model_dump(exclude={"scorers", "metrics"}),
            "eval_id": f"{submission_id}-{case_sha256[:12]}-v{ours.get('variant')}",
            "run_id": submission_id,
            "created": min(log.eval.created for log in runs),
            "dataset": EvalDataset(
                name=first.task, samples=1, sample_ids=[samples[0].id]
            ).model_dump(),
            "config": EvalConfig(
                epochs=first.config.epochs, epochs_reducer=list(reducers)
            ).model_dump(),
            "metadata": {**(first.metadata or {}), "swarmeval": ours},
            # EvalScorer and EvalMetricDefinition are not exported; the header takes their
            # JSON form, which is also how a reader sees it.
            "scorers": [
                {"name": name, "metrics": [{"name": m} for m in _METRICS]} for name in scorers
            ],
        }
    )
    log = EvalLog(
        eval=spec,
        status="success",
        samples=sorted(samples, key=lambda s: s.epoch),
    )
    try:
        recompute_metrics(log)
    except LookupError as err:
        raise VariantLogError(
            f"reducers {', '.join(reducers)}: {err}. Use Inspect reducer names: mean, median, "
            "mode, max, majority, at_least_<k>, pass_at_<k>."
        ) from err
    return log


def _check_same_variant(runs: Sequence[EvalLog]) -> None:
    def identity(log: EvalLog) -> tuple[object, ...]:
        ours = (log.eval.metadata or {}).get("swarmeval", {})
        return (
            log.eval.task,
            log.eval.task_args,
            log.eval.model,
            ours.get("variant"),
            ours.get("models"),
            ours.get("workspace"),
        )

    expected = identity(runs[0])
    for log in runs[1:]:
        if identity(log) != expected:
            raise VariantLogError(
                f"runs {runs[0].eval.run_id} and {log.eval.run_id} are not the same variant: "
                f"{expected} vs {identity(log)} (task, task_args, model, variant, models, "
                "workspace). Group runs by submission, case revision, and variant."
            )
