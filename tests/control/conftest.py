from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import grpc
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.live import EventListener
from swarmeval.control.queue import Queue
from swarmeval.control.service import ControlService
from swarmeval.events import ObjectStore
from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import (
    ControlServiceStub,
    add_ControlServiceServicer_to_server,
)

if TYPE_CHECKING:
    from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import ControlServiceAsyncStub


@pytest.fixture
async def control(
    postgres_url: str, engine: AsyncEngine, object_store: ObjectStore
) -> AsyncIterator["ControlServiceAsyncStub"]:
    """The Control API on a loopback port, over the shared Postgres and object store."""
    listener = EventListener(postgres_url)
    await listener.start()
    server = grpc.aio.server()
    add_ControlServiceServicer_to_server(
        ControlService(queue=Queue(engine), engine=engine, store=object_store, listener=listener),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        yield ControlServiceStub(channel)
    await server.stop(None)
    await listener.close()
