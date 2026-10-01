"""`swarmeval.env_state`: snapshots of environment state as `env.state` events (v1 spec §6).

Each snapshot is a shell command run in a sandbox through `ctx.sandbox`, so the command and its
full result are recorded as a `SandboxEvent` too. The `env.state` event names the snapshot and
carries its output, for queries that want state over time without parsing sandbox events.
"""

from typing import Annotated

from pydantic import BaseModel, Field

from swarmeval.runtime.extensions import ExtensionAPI, HookContext, NoState, extension
from swarmeval.runtime.records import Exec


class Snapshot(BaseModel):
    id: str
    sandbox: str
    run: Annotated[str, Field(min_length=1)]
    """Shell command, run with `sh -c`. Keep it read-only: anything it writes is recorded as a
    change made by this extension."""
    timeout_s: float = 30.0


class EnvStateConfig(BaseModel):
    snapshots: Annotated[list[Snapshot], Field(min_length=1)]
    every_turn: bool = False
    """Also snapshot after every turn, not only at run start and end."""


@extension(id="swarmeval.env_state", api_version=1, config=EnvStateConfig)
def setup(ext: ExtensionAPI[EnvStateConfig, NoState]) -> None:
    config = ext.config

    async def snapshot(ctx: HookContext[NoState], when: str) -> None:
        for shot in config.snapshots:
            command = Exec(argv=("sh", "-c", shot.run), timeout_s=shot.timeout_s)
            result = await ctx.sandbox.exec(shot.sandbox, command)
            ctx.emit(
                "env.state",
                {
                    "snapshot": shot.id,
                    "sandbox": shot.sandbox,
                    "when": when,
                    "exit_code": result.exit_code,
                    "output": result.stdout + result.stderr,
                },
            )

    @ext.on("on_run_start")
    async def at_start(ctx: HookContext[NoState]) -> None:  # pyright: ignore[reportUnusedFunction]
        await snapshot(ctx, "run_start")

    if config.every_turn:

        @ext.on("after_turn")
        async def after_turn(ctx: HookContext[NoState]) -> None:  # pyright: ignore[reportUnusedFunction]
            await snapshot(ctx, "after_turn")

    @ext.on("on_run_end")
    async def at_end(ctx: HookContext[NoState]) -> None:  # pyright: ignore[reportUnusedFunction]
        await snapshot(ctx, "run_end")
