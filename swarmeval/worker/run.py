"""One run, from claim to teardown (docs/services/orchestrator.md#run-lifecycle).

The run's case comes from its stored bundle, so the worker runs exactly what was submitted.
Sandboxes are destroyed whatever happens; a run that fails keeps every event it committed.
"""

import asyncio
import contextlib
import logging
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import grpc
import httpx2
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.bundles import bundle_key, unpack
from swarmeval.control.queue import Queue, RunRow, RunStatus
from swarmeval.core import CaseError, Variant, load_case, run_spec
from swarmeval.core.models import Scalar
from swarmeval.events import ObjectStore, PostgresRunStore, RunHeader, export_run
from swarmeval.gateway.model.client import (
    UPSTREAM_ERROR_STATUS,
    GatewaySession,
    ModelGatewayError,
)
from swarmeval.honeypot import PlacedCanary, place
from swarmeval.runtime import RunConfigError, RunLoop
from swarmeval.runtime.extensions import ExtensionError, ExtensionLoadError, load_extensions
from swarmeval.runtime.ports import AgentCaller, Caller, ExtensionCaller
from swarmeval.runtime.records import CommittedEvent
from swarmeval.runtime.tools import BUILTIN_TOOL_NAMES, BUILTIN_TOOLS
from swarmeval.runtime.writer import RunWriter
from swarmeval.sandbox import RunSandboxes, S3BlobStore, SandboxdError, SeedFile
from swarmeval.scorers import FinalStateScoring

log = logging.getLogger(__name__)

CANCELLED = "cancelled through the Control API"

_OVERRIDES = TypeAdapter(dict[str, list[Scalar]])


@dataclass(frozen=True)
class WorkerDeps:
    engine: AsyncEngine
    queue: Queue
    store: ObjectStore
    """Case bundles, blobs, and exports."""
    sandboxd: grpc.aio.Channel
    gateway_http: httpx2.AsyncClient
    gateway_grpc: grpc.aio.Channel
    cancel_poll_s: float = 2.0


@dataclass(frozen=True)
class Outcome:
    status: RunStatus
    error: str | None = None


def service_failure(err: SandboxdError | ModelGatewayError) -> Outcome:
    """A model backend that refused or failed the call (model-gateway's 502) ends the run as
    `failed`: retrying the same context gets the same answer. Anything else is the platform
    being unavailable, which ends it as `interrupted`; M2 pauses and resumes instead."""
    if isinstance(err, ModelGatewayError) and err.status == UPSTREAM_ERROR_STATUS:
        return Outcome("failed", str(err))
    return Outcome("interrupted", str(err))


async def _variant(run: RunRow, store: ObjectStore) -> Variant:
    bundle = await asyncio.to_thread(store.get, bundle_key(run.case_sha256))

    def load() -> Variant:
        with tempfile.TemporaryDirectory(prefix="swarmeval-run-") as scratch:
            overrides = _OVERRIDES.validate_python(run.overrides)
            loaded = load_case(unpack(bundle, Path(scratch)), overrides)
            return loaded.variants[run.variant]

    return await asyncio.to_thread(load)


async def _watch_cancel(queue: Queue, run_id: str, loop: RunLoop, poll_s: float) -> None:
    while True:
        await asyncio.sleep(poll_s)
        if await queue.status(run_id) == "cancelled":
            loop.stop(CANCELLED)
            return


def _isolation(runtimes: Sequence[str]) -> str:
    """The weakest runtime any sandbox got is the run's isolation level."""
    return "runc" if "runc" in runtimes else runtimes[0]


async def execute(run: RunRow, deps: WorkerDeps) -> Outcome:
    """Drives `run` to its end. Failures the run itself causes, or that its infrastructure
    causes, become an `Outcome`; anything else is a bug and propagates."""
    try:
        variant = await _variant(run, deps.store)
        extensions = load_extensions(variant.case.extensions, builtin_tools=BUILTIN_TOOL_NAMES)
    except (CaseError, ExtensionLoadError) as err:
        return Outcome("failed", f"the case no longer loads: {err}")
    canaries = place(variant.env.canaries)
    spec = replace(
        run_spec(variant, run_id=run.run_id, seed=run.epoch),
        canaries=tuple(c.info for c in canaries),
    )
    store = PostgresRunStore(
        deps.engine,
        run_id=run.run_id,
        workspace=run.workspace,
        owner_epoch=run.owner_epoch,
        sandboxes={a.id: a.sandbox_id for a in spec.agents},
    )
    writer = RunWriter(store)
    committed: list[CommittedEvent] = []
    writer.subscribe(committed.extend)
    sandboxes = RunSandboxes(deps.sandboxd, run.run_id, S3BlobStore(deps.store))
    callers: list[Caller] = [AgentCaller(a.id) for a in spec.agents]
    callers.extend(ExtensionCaller(e.instance_id) for e in extensions)
    try:
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_callback(sandboxes.destroy)
            await _create_sandboxes(run, variant, canaries, sandboxes, deps.queue)
            gateway = await stack.enter_async_context(
                GatewaySession(
                    http=deps.gateway_http,
                    channel=deps.gateway_grpc,
                    run_id=run.run_id,
                    owner_epoch=run.owner_epoch,
                    callers=callers,
                    writer=writer,
                )
            )
            loop = RunLoop(
                spec,
                writer=writer,
                model_client=gateway,
                sandbox_executor=sandboxes,
                extensions=extensions,
                tools=BUILTIN_TOOLS,
            )
            watcher = asyncio.create_task(
                _watch_cancel(deps.queue, run.run_id, loop, deps.cancel_poll_s)
            )
            try:
                outcome = await loop.run()
            finally:
                watcher.cancel()
            cancelled = outcome.status == "stopped" and outcome.reason == CANCELLED
            if not cancelled:
                await FinalStateScoring(
                    scorers=variant.case.scorers,
                    scripts=variant.scripts,
                    canaries=spec.canaries,
                    agent_sandboxes={a.id: variant.sandbox_of(a.id).id for a in spec.agents},
                    sandboxes=sandboxes,
                    writer=writer,
                ).run(committed)
    except (ExtensionError, RunConfigError) as err:
        return Outcome("failed", str(err))
    except (SandboxdError, ModelGatewayError) as err:
        outcome = service_failure(err)
        log.warning("run %s %s: %s", run.run_id, outcome.status, err, exc_info=True)
        return outcome
    await export_run(deps.engine, _header(run, variant), deps.store)
    return Outcome("cancelled" if cancelled else "done")


async def _create_sandboxes(
    run: RunRow,
    variant: Variant,
    canaries: Sequence[PlacedCanary],
    sandboxes: RunSandboxes,
    queue: Queue,
) -> None:
    await sandboxes.create_run(list(variant.sandboxes))
    runtimes: list[str] = []
    for plan in variant.sandboxes.values():
        seeds = [
            SeedFile(path=f.path, content=f.content, mode=f.mode) for f in variant.files[plan.id]
        ]
        seeds.extend(
            SeedFile(path=c.info.path, content=c.content)
            for c in canaries
            if c.info.sandbox_id == plan.id
        )
        profile = variant.env.sandbox_profiles[plan.profile]
        users = sorted(
            {
                a.os_user
                for a in variant.case.swarm.agents
                if a.os_user is not None and variant.sandbox_of(a.id).id == plan.id
            }
        )
        runtimes.append(await sandboxes.create(plan.id, profile, seeds, users))
    await queue.set_isolation(run.run_id, run.owner_epoch, _isolation(runtimes))


def _header(run: RunRow, variant: Variant) -> RunHeader:
    agents = variant.case.swarm.agents
    return RunHeader(
        run_id=run.run_id,
        case_id=run.case_id,
        variant=run.variant,
        task_args=run.task_args,
        epoch=run.epoch,
        epochs=run.epochs,
        input=variant.prompts[agents[0].id].task,
        models={a.id: a.model for a in agents},
    )
