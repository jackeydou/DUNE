"""`python -m swarmeval.analysis <job>`: batch jobs over exported runs.

- `report [--submission ID ...] [--suite LABEL ...] [--compare AXIS=A,B]`: trigger rates as
  Markdown on stdout, and with `--compare` the difference in rate between two values of an axis.
- `judge --question Q --model M (--run ID ... | --submission ID ...)`: one LLM judge verdict per
  run, stored in `analysis.judge_verdicts`; one line per run on stdout. The gateway key comes
  from SWARMEVAL_ANALYSIS_KEY.
- `eval-set --out DIR [--submission ID ...] [--reducer NAME ...]`: one Inspect `.eval` per
  variant in DIR, every `done` epoch a sample; one line per variant on stdout.
- `timeline --run ID [--agent A ...] [--from-seq N] [--to-seq N] [--html FILE]`: one run's
  events, a lane per agent, as Markdown on stdout and optionally an HTML page.
- `scan --rules FILE (--run ID ... | --submission ID ...)`: a rule set over each run's decoded
  event payloads, stored in `analysis.rule_matches`; one line per run on stdout.
- `detect --detectors FILE (--run ID ... | --submission ID ...)`: the Monitor's detectors over
  each run's events, one line per hit on stdout.
- `trace --run ID --event EVENT_ID`: the event's causal chain along `parent_id`, the run's root
  first, one line per event on stdout.
"""

import argparse
import asyncio
import os
from collections import Counter
from pathlib import Path

import httpx2

from swarmeval import config
from swarmeval.analysis import compare, timeline, trace
from swarmeval.analysis.detect import DetectorSetError, detect_rows, load_detectors
from swarmeval.analysis.evalset import describe, variant_runs, write_eval_set
from swarmeval.analysis.exports import ExportError, load_events, load_events_table, runs_of
from swarmeval.analysis.judge import Gateway, judge
from swarmeval.analysis.report import load_summaries, markdown, report
from swarmeval.analysis.scan import scan_events, store_scan
from swarmeval.db import async_engine
from swarmeval.detect.rules import RuleSetError, load_rules
from swarmeval.events import DEFAULT_REDUCERS


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m swarmeval.analysis")
    jobs = parser.add_subparsers(dest="job", required=True)

    rates = jobs.add_parser("report", help="trigger rates per case, variant, and scorer")
    rates.add_argument(
        "--submission",
        action="append",
        default=[],
        help="only runs from this submission; repeatable. Default: every run in the bucket",
    )
    rates.add_argument(
        "--suite",
        action="append",
        default=[],
        help="only runs from this suite run (the label `swarmeval.control.suite submit` "
        "prints); repeatable, and adds to --submission",
    )
    rates.add_argument(
        "--compare",
        help="AXIS=A,B: also the difference in rate between two values of one variant axis, "
        "the other axes held equal, e.g. 'paraphrased=[],[dm_ab]'",
    )
    config.add_object_store(rates)

    ask = jobs.add_parser("judge", help="ask an LLM judge one question about each run")
    ask.add_argument("--question", required=True)
    ask.add_argument("--model", required=True, help="a model name model-gateway serves")
    runs = ask.add_mutually_exclusive_group(required=True)
    runs.add_argument("--run", action="append", default=[], help="a run id; repeatable")
    runs.add_argument(
        "--submission", action="append", default=[], help="every `done` run of a submission"
    )
    ask.add_argument("--from-seq", type=int, help="first event to show the judge")
    ask.add_argument("--to-seq", type=int, help="last event to show the judge")
    ask.add_argument(
        "--gateway-url",
        default=os.environ.get("SWARMEVAL_GATEWAY_URL", "http://127.0.0.1:7080"),
        help="model-gateway's HTTP address (env SWARMEVAL_GATEWAY_URL)",
    )
    config.add_object_store(ask)
    config.add_database(ask)

    evals = jobs.add_parser("eval-set", help="one Inspect .eval per variant, epochs as samples")
    evals.add_argument("--out", type=Path, required=True, help="local directory for the logs")
    evals.add_argument(
        "--submission",
        action="append",
        default=[],
        help="only runs from this submission; repeatable. Default: every run in the bucket",
    )
    evals.add_argument(
        "--reducer",
        action="append",
        default=[],
        help="Inspect epoch reducer (mean, median, max, at_least_<k>, ...); repeatable. "
        f"Default: {', '.join(DEFAULT_REDUCERS)}",
    )
    config.add_object_store(evals)

    lanes = jobs.add_parser("timeline", help="one run's events in order, a lane per agent")
    lanes.add_argument("--run", required=True, help="the run id")
    lanes.add_argument(
        "--agent",
        action="append",
        default=[],
        help=f"only this agent's lane; repeatable. `{timeline.RUN_LANE}` is the run's own events",
    )
    lanes.add_argument("--from-seq", type=int, help="first event to show")
    lanes.add_argument("--to-seq", type=int, help="last event to show")
    lanes.add_argument("--event-chars", type=int, default=400, help="characters per event")
    lanes.add_argument("--html", type=Path, help="also write a self-contained HTML page here")
    config.add_object_store(lanes)

    scan = jobs.add_parser("scan", help="match a rule set against runs' decoded payloads")
    scan.add_argument("--rules", type=Path, required=True, help="the rule set, a YAML file")
    runs = scan.add_mutually_exclusive_group(required=True)
    runs.add_argument("--run", action="append", default=[], help="a run id; repeatable")
    runs.add_argument(
        "--submission",
        action="append",
        default=[],
        help="every exported (`done` or `cancelled`) run of a submission; repeatable",
    )
    config.add_object_store(scan)
    config.add_database(scan)

    find = jobs.add_parser("detect", help="the Monitor's detectors over exported runs' events")
    find.add_argument("--detectors", type=Path, required=True, help="the detectors, a YAML file")
    runs = find.add_mutually_exclusive_group(required=True)
    runs.add_argument("--run", action="append", default=[], help="a run id; repeatable")
    runs.add_argument(
        "--submission",
        action="append",
        default=[],
        help="every exported (`done` or `cancelled`) run of a submission; repeatable",
    )
    config.add_object_store(find)

    chain = jobs.add_parser("trace", help="an event's causal chain, from the run's root to it")
    chain.add_argument("--run", required=True, help="the run id")
    chain.add_argument("--event", required=True, help="the event id to trace")
    chain.add_argument("--event-chars", type=int, default=400, help="characters per event")
    config.add_object_store(chain)

    args = parser.parse_args()
    try:
        match args.job:
            case "report":
                comparison = compare.parse_comparison(args.compare) if args.compare else None
                summaries = load_summaries(config.object_store(args))
                rates = report(summaries, args.submission, args.suite)
                print(markdown(rates), end="")
                if comparison is not None:
                    differences = compare.compare(rates, comparison)
                    print("\n" + compare.markdown(differences, comparison), end="")
            case "judge":
                asyncio.run(_judge(args))
            case "eval-set":
                asyncio.run(_eval_set(args))
            case "timeline":
                asyncio.run(_timeline(args))
            case "trace":
                asyncio.run(_trace(args))
            case "detect":
                asyncio.run(_detect(args))
            case _:
                asyncio.run(_scan(args))
    except (
        ExportError,
        RuleSetError,
        DetectorSetError,
        trace.TraceError,
        compare.CompareError,
    ) as err:
        raise SystemExit(str(err)) from err


async def _scan(args: argparse.Namespace) -> None:
    rules = load_rules(args.rules)
    store = config.object_store(args)
    run_ids: list[str] = list(args.run)
    if args.submission:
        summaries = await asyncio.to_thread(load_summaries, store)
        run_ids = runs_of(summaries, args.submission, ["done", "cancelled"])
    engine = async_engine(config.database_url(args))
    total = 0
    try:
        for run_id in run_ids:
            rows = await load_events(store, run_id)
            matches = await asyncio.to_thread(scan_events, run_id, rows, rules)
            await store_scan(engine, run_id, rules, matches)
            total += len(matches)
            counts = Counter(m.rule_id for m in matches)
            detail = ", ".join(f"{rule}: {n}" for rule, n in sorted(counts.items()))
            print(f"{run_id}\t{len(matches)} matches" + (f": {detail}" if detail else ""))
    finally:
        await engine.dispose()
    print(f"rule set {rules.sha256()[:12]}: {total} matches in {len(run_ids)} runs")


async def _detect(args: argparse.Namespace) -> None:
    detectors = load_detectors(args.detectors)
    store = config.object_store(args)
    run_ids: list[str] = list(args.run)
    if args.submission:
        summaries = await asyncio.to_thread(load_summaries, store)
        run_ids = runs_of(summaries, args.submission, ["done", "cancelled"])
    total = 0
    for run_id in run_ids:
        hits = detect_rows(await load_events(store, run_id), detectors)
        total += len(hits)
        for hit in hits:
            print(f"{run_id}\t{','.join(hit.event_ids)}\t{hit.detector}\t{hit.detail}")
    print(f"{total} hits in {len(run_ids)} runs")


async def _trace(args: argparse.Namespace) -> None:
    table = await load_events_table(config.object_store(args), args.run)
    print(trace.text(trace.trace(table, args.event, event_chars=args.event_chars)), end="")


async def _timeline(args: argparse.Namespace) -> None:
    table = await load_events_table(config.object_store(args), args.run)
    result = timeline.timeline(
        table,
        agents=args.agent,
        from_seq=args.from_seq,
        to_seq=args.to_seq,
        event_chars=args.event_chars,
    )
    print(timeline.markdown(result), end="")
    if args.html:
        args.html.write_text(timeline.page(result), encoding="utf-8")


async def _eval_set(args: argparse.Namespace) -> None:
    store = config.object_store(args)
    summaries = await asyncio.to_thread(load_summaries, store)
    variants = variant_runs(summaries, args.submission)
    if not variants:
        raise SystemExit("no runs match; check --submission and the bucket.")
    reducers = tuple(args.reducer) or DEFAULT_REDUCERS
    print(describe(await write_eval_set(store, variants, args.out, reducers)), end="")


async def _judge(args: argparse.Namespace) -> None:
    store = config.object_store(args)
    run_ids: list[str] = list(args.run)
    if args.submission:
        summaries = await asyncio.to_thread(load_summaries, store)
        run_ids = runs_of(summaries, args.submission, ["done"])
    key = os.environ.get("SWARMEVAL_ANALYSIS_KEY")
    if not key:
        raise SystemExit(
            "set SWARMEVAL_ANALYSIS_KEY to the key model-gateway's `analysis_key_env` names."
        )
    gateway = Gateway(url=args.gateway_url, key=key)
    engine = async_engine(config.database_url(args))
    yes = 0
    try:
        async with httpx2.AsyncClient(timeout=600) as http:
            for run_id in run_ids:
                verdict = await judge(
                    run_id,
                    args.question,
                    model=args.model,
                    rows=await load_events(store, run_id),
                    gateway=gateway,
                    http=http,
                    engine=engine,
                    from_seq=args.from_seq,
                    to_seq=args.to_seq,
                )
                accepted = verdict.status == "accepted"
                yes += accepted and verdict.answer == "yes"
                detail = (
                    f"{verdict.answer} {', '.join(verdict.citations)}: {verdict.explanation}"
                    if accepted
                    else f"rejected: {verdict.rejection}"
                )
                print(f"{run_id}\t{detail}")
    finally:
        await engine.dispose()
    print(f"yes: {yes} of {len(run_ids)} runs")


if __name__ == "__main__":
    main()
