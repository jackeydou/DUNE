"""`python -m swarmeval.analysis <job>`: batch jobs over exported runs.

- `report [--submission ID ...]`: trigger rates as a Markdown table on stdout.
- `judge --question Q --model M (--run ID ... | --submission ID ...)`: one LLM judge verdict per
  run, stored in `analysis.judge_verdicts`; one line per run on stdout. The gateway key comes
  from SWARMEVAL_ANALYSIS_KEY.
"""

import argparse
import asyncio
import os

import httpx2

from swarmeval import config
from swarmeval.analysis.judge import Gateway, judge, load_events
from swarmeval.analysis.report import load_summaries, markdown, report
from swarmeval.db import async_engine


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

    args = parser.parse_args()
    if args.job == "report":
        summaries = load_summaries(config.object_store(args))
        print(markdown(report(summaries, args.submission)), end="")
    else:
        asyncio.run(_judge(args))


async def _judge(args: argparse.Namespace) -> None:
    store = config.object_store(args)
    run_ids: list[str] = list(args.run)
    if args.submission:
        summaries = await asyncio.to_thread(load_summaries, store)
        run_ids = sorted(
            r["run_id"]
            for r in summaries.to_pylist()
            if r["submission_id"] in args.submission and r["status"] == "done"
        )
        if not run_ids:
            raise SystemExit(f"submissions {', '.join(args.submission)} have no `done` runs.")
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
