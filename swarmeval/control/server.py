"""Runs the control plane: the Control API on gRPC. Migrates the database first."""

import argparse
import asyncio
import logging

import grpc

from swarmeval.config import (
    add_case_code,
    add_database,
    add_object_store,
    database_url,
    object_store,
)
from swarmeval.control.live import EventListener
from swarmeval.control.queue import Queue
from swarmeval.control.service import ControlService
from swarmeval.db import async_engine, migrate
from swarmeval.events import ObjectStore
from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import (
    add_ControlServiceServicer_to_server,
)

MAX_MESSAGE_BYTES = 64 << 20
"""Case bundles arrive in one message; gRPC's default 4 MiB is too small for real cases."""


async def serve(url: str, store: ObjectStore, listen: str, *, allow_case_code: bool) -> None:
    await asyncio.to_thread(migrate, url)
    engine = async_engine(url)
    listener = EventListener(url)
    await listener.start()
    server = grpc.aio.server(
        options=[
            ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
            ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ]
    )
    service = ControlService(
        queue=Queue(engine),
        engine=engine,
        store=store,
        listener=listener,
        allow_case_code=allow_case_code,
    )
    add_ControlServiceServicer_to_server(service, server)
    server.add_insecure_port(listen)
    await server.start()
    try:
        await server.wait_for_termination()
    finally:
        await server.stop(grace=5)
        await listener.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="swarmeval-control")
    add_database(parser)
    add_object_store(parser)
    add_case_code(parser)
    parser.add_argument("--listen", default="127.0.0.1:7090", help="Control API address")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        serve(
            database_url(args),
            object_store(args),
            args.listen,
            allow_case_code=args.allow_case_code,
        )
    )


if __name__ == "__main__":
    main()
