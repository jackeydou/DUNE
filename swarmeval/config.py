"""Command-line settings shared by the orchestrator's entry points.

Secrets come from the environment only, never from flags, so they stay out of process lists.
"""

import argparse
import os

from swarmeval.events import ObjectStore


def _env(name: str) -> str:
    try:
        return os.environ[name]
    except KeyError as err:
        raise SystemExit(
            f"environment variable {name} is not set. Set it and start again."
        ) from err


def add_database(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database-url",
        default=os.environ.get("SWARMEVAL_DATABASE_URL"),
        help="libpq URL of the orchestrator's Postgres (env SWARMEVAL_DATABASE_URL)",
    )


def database_url(args: argparse.Namespace) -> str:
    url: str | None = args.database_url
    if not url:
        raise SystemExit("set --database-url or SWARMEVAL_DATABASE_URL.")
    return url


def add_object_store(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--s3-endpoint",
        default=os.environ.get("SWARMEVAL_S3_ENDPOINT"),
        help="host:port of the S3-compatible store (env SWARMEVAL_S3_ENDPOINT)",
    )
    parser.add_argument("--s3-bucket", default=os.environ.get("SWARMEVAL_S3_BUCKET", "swarmeval"))
    parser.add_argument("--s3-scheme", choices=("http", "https"), default="https")
    parser.add_argument("--s3-region", default="us-east-1")


def object_store(args: argparse.Namespace) -> ObjectStore:
    """Keys from SWARMEVAL_S3_ACCESS_KEY and SWARMEVAL_S3_SECRET_KEY."""
    endpoint: str | None = args.s3_endpoint
    if not endpoint:
        raise SystemExit("set --s3-endpoint or SWARMEVAL_S3_ENDPOINT.")
    return ObjectStore(
        endpoint=endpoint,
        bucket=args.s3_bucket,
        access_key=_env("SWARMEVAL_S3_ACCESS_KEY"),
        secret_key=_env("SWARMEVAL_S3_SECRET_KEY"),
        scheme=args.s3_scheme,
        region=args.s3_region,
    )


def add_case_code(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-case-code",
        action="store_true",
        default=os.environ.get("SWARMEVAL_ALLOW_CASE_CODE") == "1",
        help="accept and run cases that load extensions from their own directory (`case:`). "
        "That code runs inside the worker with the worker's privileges. Off by default "
        "(env SWARMEVAL_ALLOW_CASE_CODE=1)",
    )
