"""The orchestrator with every real piece but the model: Postgres, RustFS, and sandboxd from
`tests/containers.py`, model-gateway in process with a scripted backend, and the Control API
on a local gRPC server."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import grpc
import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.live import EventListener
from swarmeval.control.queue import Queue
from swarmeval.control.service import ControlService
from swarmeval.events import ObjectStore
from swarmeval.gateway.model.app import create_app
from swarmeval.gateway.model.config import GatewayConfig
from swarmeval.gateway.model.recorder import Attachments, Recorder
from swarmeval.gateway.model.upstream import Upstreams
from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import (
    ControlServiceStub,
    add_ControlServiceServicer_to_server,
)
from swarmeval.proto.swarmeval.modelgw.v1.recorder_pb2_grpc import (
    add_RecorderServiceServicer_to_server,
)
from swarmeval.worker import Worker, WorkerDeps
from tests.gateway.mock_backend import MockBackend

if TYPE_CHECKING:
    from swarmeval.proto.swarmeval.control.v1.control_pb2_grpc import ControlServiceAsyncStub


@dataclass
class Platform:
    control: "ControlServiceAsyncStub"
    worker: Worker
    backend: MockBackend
    store: ObjectStore
    queue: Queue


@pytest.fixture
async def bare_deps(engine: AsyncEngine, object_store: ObjectStore) -> AsyncIterator[WorkerDeps]:
    """Postgres and RustFS only: sandboxd and model-gateway point nowhere, for tests that
    replace `execute`."""
    async with (
        grpc.aio.insecure_channel("127.0.0.1:9") as nowhere,
        httpx2.AsyncClient(base_url="http://127.0.0.1:9") as http,
    ):
        yield WorkerDeps(
            engine=engine,
            queue=Queue(engine),
            store=object_store,
            sandboxd=nowhere,
            gateway_http=http,
            gateway_grpc=nowhere,
        )


@pytest.fixture
async def platform(
    postgres_url: str, engine: AsyncEngine, object_store: ObjectStore, sandboxd: str
) -> AsyncIterator[Platform]:
    async with _platform(postgres_url, engine, object_store, sandboxd) as started:
        yield started


@pytest.fixture
async def case_code_platform(
    postgres_url: str, engine: AsyncEngine, object_store: ObjectStore, sandboxd: str
) -> AsyncIterator[Platform]:
    """A deployment started with `--allow-case-code`."""
    async with _platform(
        postgres_url, engine, object_store, sandboxd, allow_case_code=True
    ) as started:
        yield started


@asynccontextmanager
async def _platform(
    postgres_url: str,
    engine: AsyncEngine,
    object_store: ObjectStore,
    sandboxd: str,
    *,
    allow_case_code: bool = False,
) -> AsyncGenerator[Platform]:
    backend = MockBackend()
    config = GatewayConfig.model_validate(
        {
            "backends": {"mock": {"base_url": "http://backend/v1", "max_retries": 0}},
            "models": {
                "mock-model": {"backend": "mock", "upstream_model": "Org/Mock"},
                "qwen3-8b": {"backend": "mock", "upstream_model": "Org/Mock"},
            },
        }
    )
    upstreams = Upstreams(config, transports={"mock": httpx2.ASGITransport(app=backend.app())})
    attachments = Attachments(ack_timeout_s=30)
    listener = EventListener(postgres_url)
    await listener.start()
    queue = Queue(engine)
    server = grpc.aio.server()
    add_RecorderServiceServicer_to_server(Recorder(attachments), server)
    add_ControlServiceServicer_to_server(
        ControlService(
            queue=queue,
            engine=engine,
            store=object_store,
            listener=listener,
            allow_case_code=allow_case_code,
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    gateway_app = create_app(attachments, upstreams)
    async with (
        grpc.aio.insecure_channel(f"127.0.0.1:{port}") as local,
        grpc.aio.insecure_channel(sandboxd) as sandboxd_channel,
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=gateway_app), base_url="http://gw", timeout=None
        ) as http,
    ):
        deps = WorkerDeps(
            engine=engine,
            queue=queue,
            store=object_store,
            sandboxd=sandboxd_channel,
            gateway_http=http,
            gateway_grpc=local,
            allow_case_code=allow_case_code,
        )
        yield Platform(
            control=ControlServiceStub(local),
            worker=Worker(deps, owner_id="worker_e2e"),
            backend=backend,
            store=object_store,
            queue=queue,
        )
    await server.stop(None)
    await listener.close()
    await upstreams.close()
