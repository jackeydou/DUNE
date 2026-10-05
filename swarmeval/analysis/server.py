"""Runs the analysis service: `AnalysisService` on gRPC, for edge. The database must already be
migrated; the control plane migrates it when it starts."""

import argparse
import logging
import os

import grpc
import httpx2

from swarmeval.analysis.judge import Gateway
from swarmeval.analysis.service import AnalysisService
from swarmeval.config import (
    add_database,
    add_object_store,
    database_url,
    object_store,
    run_service,
)
from swarmeval.db import async_engine
from swarmeval.events import ObjectStore
from swarmeval.mtls import (
    EDGE,
    Identity,
    add_mtls,
    add_port,
    check_listen,
    http_client_tls,
    identity,
    interceptors,
)
from swarmeval.proto.swarmeval.analysis.v1.analysis_pb2_grpc import (
    add_AnalysisServiceServicer_to_server,
)

JUDGE_TIMEOUT_S = 600
ANALYSIS_KEY_ENV = "SWARMEVAL_ANALYSIS_KEY"


CALLERS = (EDGE,)
"""Who may call the analysis service: edge, for users."""


async def serve(
    url: str,
    store: ObjectStore,
    listen: str,
    gateway: Gateway | None,
    *,
    gateway_url: str,
    mtls: Identity | None,
) -> None:
    """`gateway_url` is checked against `mtls` also when `gateway` is None, so a wrong URL
    shows at start, not when a key is added later."""
    check_listen("--listen", listen, mtls)
    verify = http_client_tls("--gateway-url", gateway_url, mtls)
    engine = async_engine(url)
    async with httpx2.AsyncClient(timeout=JUDGE_TIMEOUT_S, verify=verify) as http:
        service = AnalysisService(engine=engine, store=store, http=http, gateway=gateway)
        await service.start()
        server = grpc.aio.server(interceptors=interceptors(mtls, CALLERS))
        add_AnalysisServiceServicer_to_server(service, server)
        add_port(server, listen, mtls)
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
    add_mtls(parser)
    parser.add_argument(
        "--listen",
        default="127.0.0.1:7091",
        help="AnalysisService address. Without --mtls-cert it must be a loopback address",
    )
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("SWARMEVAL_GATEWAY_URL", "http://127.0.0.1:7080"),
        help="model-gateway's HTTP address, for the judge (env SWARMEVAL_GATEWAY_URL); https "
        "with --mtls-cert",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    key = os.environ.get(ANALYSIS_KEY_ENV)
    if not key:
        logging.getLogger(__name__).warning(
            "%s is not set: Judge requests will be refused", ANALYSIS_KEY_ENV
        )
    gateway = Gateway(url=args.gateway_url, key=key) if key else None
    run_service(
        serve(
            database_url(args),
            object_store(args),
            args.listen,
            gateway,
            gateway_url=args.gateway_url,
            mtls=identity(args),
        )
    )


if __name__ == "__main__":
    main()
