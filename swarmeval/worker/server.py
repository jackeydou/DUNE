"""Runs a worker: claims runs from the queue and drives them."""

import argparse
import asyncio
import logging
import socket

import grpc
import httpx2

from swarmeval.config import add_database, add_object_store, database_url, object_store
from swarmeval.control.queue import Queue
from swarmeval.db import async_engine
from swarmeval.events import ObjectStore
from swarmeval.worker.run import WorkerDeps
from swarmeval.worker.worker import Worker


async def serve(
    url: str,
    store: ObjectStore,
    *,
    sandboxd: str,
    gateway_http: str,
    gateway_grpc: str,
    owner_id: str,
    max_runs: int,
) -> None:
    engine = async_engine(url)
    async with (
        grpc.aio.insecure_channel(sandboxd) as sandboxd_channel,
        grpc.aio.insecure_channel(gateway_grpc) as gateway_channel,
        # No read timeout: a model call lasts as long as the model takes.
        httpx2.AsyncClient(base_url=gateway_http, timeout=httpx2.Timeout(30.0, read=None)) as http,
    ):
        deps = WorkerDeps(
            engine=engine,
            queue=Queue(engine),
            store=store,
            sandboxd=sandboxd_channel,
            gateway_http=http,
            gateway_grpc=gateway_channel,
        )
        try:
            await Worker(deps, owner_id=owner_id, max_runs=max_runs).serve()
        finally:
            await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="swarmeval-worker")
    add_database(parser)
    add_object_store(parser)
    parser.add_argument("--sandboxd", default="127.0.0.1:7071", help="sandboxd gRPC address")
    parser.add_argument(
        "--gateway-http", default="http://127.0.0.1:7080", help="model-gateway HTTP base URL"
    )
    parser.add_argument("--gateway-grpc", default="127.0.0.1:7081", help="model-gateway gRPC")
    parser.add_argument(
        "--worker-id",
        default=socket.gethostname(),
        help="stable across restarts; runs it owned are marked interrupted when it starts",
    )
    parser.add_argument("--max-runs", type=int, default=4, help="runs executed at once")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        serve(
            database_url(args),
            object_store(args),
            sandboxd=args.sandboxd,
            gateway_http=args.gateway_http,
            gateway_grpc=args.gateway_grpc,
            owner_id=args.worker_id,
            max_runs=args.max_runs,
        )
    )


if __name__ == "__main__":
    main()
