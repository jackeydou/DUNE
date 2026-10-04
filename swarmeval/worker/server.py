"""Runs a worker: claims runs from the queue and drives them."""

import argparse
import asyncio
import logging
import math
import socket

import httpx2

from swarmeval.config import (
    add_case_code,
    add_database,
    add_object_store,
    database_url,
    object_store,
    run_service,
)
from swarmeval.control.queue import LEASE_S, Queue
from swarmeval.db import async_engine
from swarmeval.events import ObjectStore
from swarmeval.mtls import Identity, add_mtls, channel, http_client_tls, identity
from swarmeval.worker.run import WorkerDeps
from swarmeval.worker.worker import Worker, WorkerHalted, WorkerIdInUse, WorkerIdLost

log = logging.getLogger(__name__)


async def serve(
    url: str,
    store: ObjectStore,
    *,
    sandboxd: str,
    gateway_http: str,
    gateway_grpc: str,
    owner_id: str,
    max_runs: int,
    allow_case_code: bool,
    lease_s: float,
    mtls: Identity | None,
    stay_halted: bool = False,
) -> None:
    """Serves until cancelled or stopped by `WorkerIdInUse`, `WorkerIdLost`, or `WorkerHalted`.
    With `stay_halted`, a halted worker does not return: it idles, holding no run and claiming
    none, so a supervisor's restart policy cannot put it back on the host it found broken."""
    verify = http_client_tls("--gateway-http", gateway_http, mtls)
    engine = async_engine(url)
    async with (
        channel(sandboxd, mtls) as sandboxd_channel,
        channel(gateway_grpc, mtls) as gateway_channel,
        httpx2.AsyncClient(
            base_url=gateway_http,
            # No read timeout: a model call lasts as long as the model takes.
            timeout=httpx2.Timeout(30.0, read=None),
            verify=verify,
        ) as http,
    ):
        deps = WorkerDeps(
            engine=engine,
            queue=Queue(engine),
            store=store,
            sandboxd=sandboxd_channel,
            gateway_http=http,
            gateway_grpc=gateway_channel,
            allow_case_code=allow_case_code,
        )
        try:
            await Worker(deps, owner_id=owner_id, max_runs=max_runs, lease_s=lease_s).serve()
        except WorkerHalted as err:
            if not stay_halted:
                raise
            log.error(
                "worker %s halted: %s It stays up and claims nothing (--stay-halted). Fix the "
                "host, then restart the worker.",
                owner_id,
                err,
            )
        finally:
            await engine.dispose()
    await asyncio.Event().wait()


def lease_seconds(text: str) -> float:
    """`--lease-s`: a finite number of seconds above zero. A lease of zero or less runs out as it
    is taken, so workers would take each other's runs over endlessly."""
    try:
        value = float(text)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"`{text}` is not a number of seconds") from err
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(
            f"`{text}` is not a lease length; give a finite number of seconds above 0, such as "
            "the default 30"
        )
    return value


def main() -> None:
    parser = argparse.ArgumentParser(prog="swarmeval-worker")
    add_database(parser)
    add_object_store(parser)
    add_case_code(parser)
    add_mtls(parser)
    parser.add_argument("--sandboxd", default="127.0.0.1:7071", help="sandboxd gRPC address")
    parser.add_argument(
        "--gateway-http",
        default="http://127.0.0.1:7080",
        help="model-gateway HTTP base URL; https with --mtls-cert",
    )
    parser.add_argument("--gateway-grpc", default="127.0.0.1:7081", help="model-gateway gRPC")
    parser.add_argument(
        "--worker-id",
        default=socket.gethostname(),
        help="unique among running workers, and the same across restarts: on start, runs this "
        "id owned are marked interrupted and rerun. A second worker with an id in use refuses "
        "to start",
    )
    parser.add_argument("--max-runs", type=int, default=4, help="runs executed at once")
    parser.add_argument(
        "--lease-s",
        type=lease_seconds,
        default=LEASE_S,
        help="lease on each run this worker claims, renewed every third of it. Once a lease has "
        "run out, any worker may take the run over",
    )
    parser.add_argument(
        "--stay-halted",
        action="store_true",
        help="when a run finds this host broken (a failed isolation self-check), stay up "
        "without claiming runs instead of exiting. For deployments that restart an exited "
        "worker: a restarted one would claim the next run on the same host",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        run_service(
            serve(
                database_url(args),
                object_store(args),
                sandboxd=args.sandboxd,
                gateway_http=args.gateway_http,
                gateway_grpc=args.gateway_grpc,
                owner_id=args.worker_id,
                max_runs=args.max_runs,
                allow_case_code=args.allow_case_code,
                lease_s=args.lease_s,
                mtls=identity(args),
                stay_halted=args.stay_halted,
            )
        )
    except (WorkerIdInUse, WorkerIdLost, WorkerHalted) as err:
        raise SystemExit(str(err)) from err


if __name__ == "__main__":
    main()
