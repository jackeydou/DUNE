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
from swarmeval.gateway.model.upstream import Upstreams
from swarmeval.proto.swarmeval.modelgw.v1.recorder_pb2_grpc import (
    add_RecorderServiceServicer_to_server,
)


async def serve(config: GatewayConfig, *, http: str, grpc_address: str) -> None:
    """Serves until cancelled. `http` and `grpc_address` are `host:port`."""
    attachments = Attachments()
    upstreams = Upstreams(config)
    server = grpc.aio.server()
    add_RecorderServiceServicer_to_server(Recorder(attachments), server)
    server.add_insecure_port(grpc_address)
    host, _, port = http.rpartition(":")
    web = uvicorn.Server(
        uvicorn.Config(create_app(attachments, upstreams), host=host, port=int(port))
    )
    await server.start()
    try:
        await web.serve()
    finally:
        await server.stop(grace=5)
        await upstreams.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="swarmeval-model-gateway")
    parser.add_argument("--config", type=Path, required=True, help="backends and models, YAML")
    parser.add_argument("--http", default="127.0.0.1:7080", help="OpenAI-compatible API address")
    parser.add_argument("--grpc", default="127.0.0.1:7081", help="Recorder service address")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(GatewayConfig.load(args.config), http=args.http, grpc_address=args.grpc))


if __name__ == "__main__":
    main()
