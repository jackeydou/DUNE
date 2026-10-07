"""Runs the control plane: the Control API on gRPC. Migrates the database first."""

import argparse
import asyncio
import logging

import grpc
import httpx2

from swarmeval.config import (
    add_case_code,
    add_database,
    add_object_store,
    database_url,
    object_store,
    run_service,
)
from swarmeval.control.bundles import MAX_BUNDLE_BYTES
from swarmeval.control.live import EventListener
from swarmeval.control.models import GatewayModels
from swarmeval.control.queue import Queue
from swarmeval.control.service import ControlService
from swarmeval.db import async_engine, migrate
from swarmeval.events import ObjectStore
from swarmeval.mtls import (
    EDGE,
    OPERATOR,
    Identity,
    add_mtls,
    add_port,
    check_listen,
    http_client_tls,
    identity,
    interceptors,
)
from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import (
    add_ControlServiceServicer_to_server,
)

MAX_MESSAGE_BYTES = MAX_BUNDLE_BYTES + (1 << 20)
"""Case bundles arrive in one message; gRPC's default 4 MiB is too small for real cases. The
megabyte over the bundle limit is for the rest of the message (field framing, the actor, a
note, a suite file), so a bundle of exactly the limit still arrives."""


CALLERS = (EDGE, OPERATOR)
"""Who may call the Control API: edge for users, and operators' own tools."""


async def serve(
    url: str,
    store: ObjectStore,
    listen: str,
    *,
    gateway_http: str,
    allow_case_code: bool,
    mtls: Identity | None,
) -> None:
    check_listen("--listen", listen, mtls)
    verify = http_client_tls("--gateway-http", gateway_http, mtls)
    await asyncio.to_thread(migrate, url)
    engine = async_engine(url)
    listener = EventListener(url)
    await listener.start()
    server = grpc.aio.server(
        interceptors=interceptors(mtls, CALLERS),
        options=[
            ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
            ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ],
    )
    gateway = httpx2.AsyncClient(base_url=gateway_http, timeout=10.0, verify=verify)
    service = ControlService(
        queue=Queue(engine),
        engine=engine,
        store=store,
        listener=listener,
        models=GatewayModels(gateway),
        allow_case_code=allow_case_code,
    )
    add_ControlServiceServicer_to_server(service, server)
    add_port(server, listen, mtls)
    await server.start()
    try:
        await server.wait_for_termination()
    finally:
        await server.stop(grace=5)
        await gateway.aclose()
        await listener.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="swarmeval-control")
    add_database(parser)
    add_object_store(parser)
    add_case_code(parser)
    add_mtls(parser)
    parser.add_argument(
        "--listen",
        default="127.0.0.1:7090",
        help="Control API address. Without --mtls-cert it must be a loopback address",
    )
    parser.add_argument(
        "--gateway-http",
        default="http://127.0.0.1:7080",
        help="model-gateway HTTP base URL, asked which models runs may use; https with --mtls-cert",
    )
    args = parser.parse_args()
    mtls = identity(args)
    logging.basicConfig(level=logging.INFO)
    run_service(
        serve(
            database_url(args),
            object_store(args),
            args.listen,
            gateway_http=args.gateway_http,
            allow_case_code=args.allow_case_code,
            mtls=mtls,
        )
    )


if __name__ == "__main__":
    main()
