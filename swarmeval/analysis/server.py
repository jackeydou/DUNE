"""Runs the analysis service: `AnalysisService` on gRPC, for edge. The database must already be
migrated; the control plane migrates it when it starts."""

import argparse
import asyncio
import logging
import os

import grpc
import httpx2

from swarmeval.analysis.judge import Gateway
from swarmeval.analysis.service import AnalysisService
from swarmeval.config import add_database, add_object_store, database_url, object_store
from swarmeval.db import async_engine
from swarmeval.events import ObjectStore
from swarmeval.proto.swarmeval.analysis.v1.analysis_pb2_grpc import (
    add_AnalysisServiceServicer_to_server,
)

JUDGE_TIMEOUT_S = 600
ANALYSIS_KEY_ENV = "SWARMEVAL_ANALYSIS_KEY"


async def serve(url: str, store: ObjectStore, listen: str, gateway: Gateway | None) -> None:
    engine = async_engine(url)
    async with httpx2.AsyncClient(timeout=JUDGE_TIMEOUT_S) as http:
        service = AnalysisService(engine=engine, store=store, http=http, gateway=gateway)
        await service.start()
        server = grpc.aio.server()
        add_AnalysisServiceServicer_to_server(service, server)
        server.add_insecure_port(listen)
        await server.start()
        try:
            await server.wait_for_termination()
        finally:
            await server.stop(grace=5)
            await service.close()
            await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="swarmeval-analysis")
    add_database(parser)
    add_object_store(parser)
    parser.add_argument("--listen", default="127.0.0.1:7091", help="AnalysisService address")
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("SWARMEVAL_GATEWAY_URL", "http://127.0.0.1:7080"),
        help="model-gateway's HTTP address, for the judge (env SWARMEVAL_GATEWAY_URL)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    key = os.environ.get(ANALYSIS_KEY_ENV)
    if not key:
        logging.getLogger(__name__).warning(
            "%s is not set: Judge requests will be refused", ANALYSIS_KEY_ENV
        )
    gateway = Gateway(url=args.gateway_url, key=key) if key else None
    asyncio.run(serve(database_url(args), object_store(args), args.listen, gateway))


if __name__ == "__main__":
    main()
