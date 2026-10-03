"""`python -m swarmeval.analysis <job>`: batch jobs over exported runs.

- `report [--submission ID ...]`: trigger rates as a Markdown table on stdout.
- `judge --question Q --model M (--run ID ... | --submission ID ...)`: one LLM judge verdict per
  run, stored in `analysis.judge_verdicts`; one line per run on stdout. The gateway key comes
  from SWARMEVAL_ANALYSIS_KEY.
- `eval-set --out DIR [--submission ID ...] [--reducer NAME ...]`: one Inspect `.eval` per
  variant in DIR, every `done` epoch a sample; one line per variant on stdout.
- `timeline --run ID [--agent A ...] [--from-seq N] [--to-seq N] [--html FILE]`: one run's
  events, a lane per agent, as Markdown on stdout and optionally an HTML page.
- `scan --rules FILE (--run ID ... | --submission ID ...)`: a rule set over each run's decoded
  event payloads, stored in `analysis.rule_matches`; one line per run on stdout.
"""

import argparse
import asyncio
import os
from collections import Counter
from pathlib import Path

import httpx2

from swarmeval import config
from swarmeval.analysis import timeline
from swarmeval.analysis.evalset import describe, variant_runs, write_eval_set
from swarmeval.analysis.exports import ExportError, load_events, load_events_table, runs_of
from swarmeval.analysis.judge import Gateway, judge
from swarmeval.analysis.report import load_summaries, markdown, report
from swarmeval.analysis.rules import RuleSetError, load_rules
from swarmeval.analysis.scan import scan_events, store_scan
from swarmeval.db import async_engine
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

    args = parser.parse_args()
    try:
        match args.job:
            case "report":
                summaries = load_summaries(config.object_store(args))
                print(markdown(report(summaries, args.submission)), end="")
            case "judge":
                asyncio.run(_judge(args))
            case "eval-set":
                asyncio.run(_eval_set(args))
            case "timeline":
                asyncio.run(_timeline(args))
            case _:
                asyncio.run(_scan(args))
    except (ExportError, RuleSetError) as err:
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
