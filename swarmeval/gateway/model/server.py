"""Runs model-gateway: the HTTP API and the Recorder gRPC service in one asyncio process."""

import argparse
import asyncio
import logging
from pathlib import Path

import grpc
import uvicorn

from swarmeval.gateway.model.app import create_app
from swarmeval.gateway.model.config import GatewayConfig
from swarmeval.gateway.model.recorder import Attachments, Recorder
from swarmeval.gateway.model.tls import uvicorn_config
from swarmeval.gateway.model.upstream import Upstreams
from swarmeval.mtls import (
    ANALYSIS,
    CONTROL,
    WORKER,
    Identity,
    add_mtls,
    add_port,
    check_listen,
    identity,
    interceptors,
)
from swarmeval.proto.swarmeval.modelgw.v1.recorder_pb2_grpc import (
    add_RecorderServiceServicer_to_server,
)

CALLERS = (WORKER, ANALYSIS)
"""Who may call the gateway: workers for runs, analysis for its judge."""
LISTING = (CONTROL,)
"""Who may only list the models: the control plane, checking the models a submission names."""


async def serve(
    config: GatewayConfig, *, http: str, grpc_address: str, mtls: Identity | None
) -> None:
    """Serves until cancelled. `http` and `grpc_address` are `host:port`."""
    check_listen("--http", http, mtls)
    check_listen("--grpc", grpc_address, mtls)
    attachments = Attachments()
    upstreams = Upstreams(config)
    server = grpc.aio.server(interceptors=interceptors(mtls, CALLERS))
    add_RecorderServiceServicer_to_server(Recorder(attachments), server)
    add_port(server, grpc_address, mtls)
    host, _, port = http.rpartition(":")
    app = create_app(attachments, upstreams, config.analysis_key())
    web = uvicorn.Server(uvicorn_config(app, host, int(port), mtls, CALLERS, LISTING))
    await server.start()
    try:
        await web.serve()
    finally:
        await server.stop(grace=5)
        await upstreams.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="swarmeval-model-gateway")
    parser.add_argument("--config", type=Path, required=True, help="backends and models, YAML")
    parser.add_argument(
        "--http",
        default="127.0.0.1:7080",
        help="OpenAI-compatible API address. Without --mtls-cert it must be a loopback address",
    )
    parser.add_argument(
        "--grpc",
        default="127.0.0.1:7081",
        help="Recorder service address. Without --mtls-cert it must be a loopback address",
    )
    add_mtls(parser)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        serve(
            GatewayConfig.load(args.config),
            http=args.http,
            grpc_address=args.grpc,
            mtls=identity(args),
        )
    )


if __name__ == "__main__":
    main()
