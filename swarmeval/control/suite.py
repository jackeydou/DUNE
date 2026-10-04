"""`python -m swarmeval.control.suite`: check a suite, or submit it through the Control API.

- `check SUITE`: loads the suite and every case in it, and prints the runs it would queue.
- `submit SUITE [--control HOST:PORT]`: sends the suite with its cases to `SubmitSuite`, which
  queues each case as its own submission, all under one suite label `<id>.<8 hex>`, and prints
  the label for `python -m swarmeval.analysis report --suite`.

An operator's tool on the internal network; users run `swarm run SUITE` through edge. The
control plane loads the suite again and queues it whole or not at all. The format:
docs/case-format.md#suites.
"""

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import grpc

from swarmeval.control.bundles import pack
from swarmeval.control.server import MAX_MESSAGE_BYTES
from swarmeval.core import LoadedSuite, SuiteError, load_suite
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb
from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import ControlServiceStub

if TYPE_CHECKING:
    from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import ControlServiceAsyncStub


@dataclass(frozen=True)
class Submitted:
    case_id: str
    submission_id: str
    runs: int


def request(suite: LoadedSuite) -> pb.SubmitSuiteRequest:
    """The suite file and one bundle per distinct `cases[].path`."""
    return pb.SubmitSuiteRequest(
        suite_yaml=Path(suite.source).read_text(encoding="utf-8"),
        case_bundles={e.path: pack(e.dir) for e in suite.entries},
    )


async def submit(
    suite: LoadedSuite, control: "ControlServiceAsyncStub"
) -> tuple[str, list[Submitted]]:
    """Submits `suite`; returns its label and the submissions, in the suite's order."""
    response = await control.SubmitSuite(await asyncio.to_thread(request, suite))
    return response.suite, [
        Submitted(s.case_id, s.submission_id, len(s.run_ids)) for s in response.submissions
    ]


def plan(suite: LoadedSuite) -> str:
    lines = [
        f"{e.case.id}: {len(e.case.variants)} variant(s) x {e.epochs or e.case.epochs} "
        f"epoch(s) = {e.runs} runs ({e.dir})"
        for e in suite.entries
    ]
    lines.append(f"suite {suite.id}: {sum(e.runs for e in suite.entries)} runs")
    return "\n".join(lines)


async def _submit(suite: LoadedSuite, address: str) -> None:
    options = [("grpc.max_send_message_length", MAX_MESSAGE_BYTES)]
    async with grpc.aio.insecure_channel(address, options=options) as channel:
        label, done = await submit(suite, ControlServiceStub(channel))
    for s in done:
        print(f"{s.case_id}: submission {s.submission_id}, {s.runs} runs")
    print(f"suite label: {label}")
    print(f"report: python -m swarmeval.analysis report --suite {label}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m swarmeval.control.suite")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="load the suite and its cases; submit nothing")
    check.add_argument("suite", type=Path)
    send = commands.add_parser("submit", help="submit every case of the suite")
    send.add_argument("suite", type=Path)
    send.add_argument("--control", default="127.0.0.1:7090", help="Control API address")
    args = parser.parse_args()
    try:
        suite = load_suite(args.suite)
    except SuiteError as err:
        raise SystemExit(str(err)) from err
    print(plan(suite))
    if args.command == "submit":
        try:
            asyncio.run(_submit(suite, args.control))
        except grpc.aio.AioRpcError as err:
            raise SystemExit(
                f"the Control API refused suite {suite.source}: {err.code().name}: "
                f"{err.details()}. Nothing was queued."
            ) from err


if __name__ == "__main__":
    main()
