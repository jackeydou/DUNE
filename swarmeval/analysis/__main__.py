"""`python -m swarmeval.analysis report [--submission ID ...]`: trigger rates as a Markdown
table on stdout."""

import argparse

from swarmeval import config
from swarmeval.analysis.report import load_summaries, markdown, report


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
    args = parser.parse_args()
    summaries = load_summaries(config.object_store(args))
    print(markdown(report(summaries, args.submission)), end="")


if __name__ == "__main__":
    main()
