"""The sandboxes of a run whose case gives no agent one (spec/2026-10-07-optional-sandbox).

Stands where `RunSandboxes` would, so the run never calls sandboxd: nothing to create, diff, or
destroy. A command can only come from an extension through `ctx.sandbox`; it is refused, which
fails the run.
"""

from swarmeval.runtime import RunConfigError
from swarmeval.runtime.records import Exec, ExecResult, FsChange
from swarmeval.sandbox.client import FileContent


class NoSandboxes:
    def __init__(self, run_id: str) -> None:
        self._run_id = run_id

    async def exec(
        self, sandbox_id: str, os_user: str | None, command: Exec, *, call_id: str
    ) -> ExecResult:
        raise self._refuse(sandbox_id, f"`{' '.join(command.argv)}`")

    async def read_file(self, sandbox_id: str, path: str, *, max_bytes: int = 0) -> FileContent:
        raise self._refuse(sandbox_id, f"a read of `{path}`")

    async def final_diff(self) -> dict[str, tuple[FsChange, ...]]:
        return {}

    async def destroy(self) -> None:
        return None

    def _refuse(self, sandbox_id: str, what: str) -> RunConfigError:
        return RunConfigError(
            f"run `{self._run_id}` has no sandboxes, since every agent of its case has "
            f"`sandbox: none`, so {what} cannot run in sandbox `{sandbox_id}`. Give an agent a "
            "sandbox, or drop the extension config that names one."
        )
