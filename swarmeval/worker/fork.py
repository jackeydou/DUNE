"""Starting a fork in the worker (docs/services/orchestrator.md#forks, M2 spec decisions 8, 9):
the source's checkpoint and contexts, and its sandboxes as they were at the fork point.

Sandboxes are rebuilt from the image and seed files, then each key path is put back as the
recorded changes left it: for every path, its last change up to the fork point. A file comes
back from the blob store when sandboxd stored its content (up to 1 MiB); a directory or a
deletion needs none. A file stored by hash only, a symlink, or anything else cannot come back,
and makes the fork `fs_partial`. Background processes never come back, and nothing claims
`exact`.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from swarmeval.control.queue import RunRow
from swarmeval.db import events
from swarmeval.detect.view import EventView, view_of_row
from swarmeval.events.fork import Ancestor, lineage, point_at, tip_event_id
from swarmeval.runtime.fork import EDITS, ForkStart, cross_run, from_source
from swarmeval.runtime.records import FsChange
from swarmeval.sandbox import RestoreEntry, RunSandboxes
from swarmeval.sandbox.blobs import S3BlobStore

Fidelity = Literal["fs_restored", "fs_partial"]


@dataclass
class RestorePlan:
    remove: list[str] = field(default_factory=list[str])
    dirs: list[RestoreEntry] = field(default_factory=list[RestoreEntry])
    files: list[tuple[str, FsChange]] = field(default_factory=list[tuple[str, FsChange]])
    lost: list[str] = field(default_factory=list[str])
    """Paths whose state at the fork point cannot be restored, and why."""


async def lineage_views(engine: AsyncEngine, ancestors: Sequence[Ancestor]) -> list[EventView]:
    """The ancestors' events up to their fork points, in order, as detectors see them: what
    the fork's sandboxes are restored from, and what its scorers read before its own events."""
    views: list[EventView] = []
    async with engine.connect() as conn:
        for ancestor in ancestors:
            query = (
                select(
                    events.c.event_id,
                    events.c.seq,
                    events.c.ts,
                    events.c.type,
                    events.c.agent_id,
                    events.c.sandbox_id,
                    events.c.payload,
                )
                .where(events.c.run_id == ancestor.run_id)
                .order_by(events.c.seq)
            )
            if ancestor.up_to is not None:
                query = query.where(events.c.seq <= ancestor.up_to)
            for row in (await conn.execute(query)).mappings():
                views.append(view_of_row({**row, "payload": json.dumps(row["payload"])}))
    return views


def last_changes(views: Sequence[EventView]) -> dict[tuple[str, str], FsChange]:
    """Each (sandbox, path)'s last recorded change."""
    latest: dict[tuple[str, str], FsChange] = {}
    for view in views:
        for change in view.changes:
            assert change.sandbox_id is not None, "stored tool events name their sandbox"
            latest[(change.sandbox_id, change.change.path)] = change.change
    return latest


def plan(changes: dict[tuple[str, str], FsChange], sandbox_id: str) -> RestorePlan:
    result = RestorePlan()
    for (sandbox, path), change in sorted(changes.items()):
        if sandbox != sandbox_id:
            continue
        match change:
            case FsChange(op="delete"):
                result.remove.append(path)
            case FsChange(kind="dir"):
                result.dirs.append(RestoreEntry(path, change.mode & 0o777, change.uid))
            case FsChange(kind="file", content_stored=True, after_sha256=str()):
                result.files.append((path, change))
            case FsChange(kind="file"):
                result.lost.append(f"{path}: content stored by hash only ({change.size} bytes)")
            case _:
                result.lost.append(f"{path}: a {change.kind}, which is not restored")
    return result


async def restore(
    sandboxes: RunSandboxes,
    blobs: S3BlobStore,
    changes: dict[tuple[str, str], FsChange],
    sandbox_ids: Sequence[str],
) -> tuple[Fidelity, list[str]]:
    """Restores every sandbox before its first command. Returns the fidelity and what could
    not be restored."""
    lost: list[str] = []
    for sandbox_id in sandbox_ids:
        p = plan(changes, sandbox_id)
        files: list[RestoreEntry] = []
        for path, change in p.files:
            assert change.after_sha256 is not None, "matched with a stored hash"
            content = await blobs.get(change.after_sha256)
            files.append(RestoreEntry(path, change.mode & 0o777 or 0o644, change.uid, content))
        unowned = await sandboxes.restore(sandbox_id, remove=p.remove, dirs=p.dirs, files=files)
        lost.extend(f"{sandbox_id}:{item}" for item in p.lost)
        lost.extend(f"{sandbox_id}:{path}: owner not restored" for path in unowned)
    return ("fs_partial" if lost else "fs_restored"), lost


async def load_fork(engine: AsyncEngine, run: RunRow) -> tuple[ForkStart, list[Ancestor], str]:
    """The fork's start without its fidelity, which the sandbox restore decides; its sources;
    and the source event it goes on from, as a cross-run reference."""
    assert run.forked_from is not None and run.fork_seq is not None, "called for forks"
    point = await point_at(engine, run.forked_from, run.fork_seq)
    start = ForkStart(
        source_run_id=run.forked_from,
        fork_seq=point.seq,
        checkpoint=from_source(point.checkpoint, run.forked_from),
        contexts=point.contexts,
        edits=EDITS.validate_python(run.fork_edits or []),
        fidelity="fs_partial",
    )
    ancestors = (await lineage(engine, run.run_id))[:-1]
    tip = cross_run(run.forked_from, await tip_event_id(engine, run.forked_from, point.seq))
    return start, ancestors, tip
