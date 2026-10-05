"""Tool calls of exported runs, filtered: the structured search the console's filters use
(docs/services/analysis.md#capabilities)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from swarmeval.analysis import query
from swarmeval.events import ObjectStore

DEFAULT_LIMIT = 200
MAX_LIMIT = 2_000
RESULT_CHARS = 2_000


@dataclass(frozen=True)
class ToolCall:
    run_id: str
    seq: int
    event_id: str
    time: datetime
    agent_id: str | None
    tool: str
    arguments_json: str
    result: str
    error: str | None


_SEARCH = """
    SELECT run_id, seq, event_id, ts AS time, agent_id,
           coalesce(json_extract_string(payload, '$.function'), '') AS tool,
           coalesce(json_extract(payload, '$.arguments')::VARCHAR, '{}') AS arguments_json,
           left(coalesce(json_extract_string(payload, '$.result'), ''), $result_chars) AS result,
           json_extract_string(payload, '$.error.message') AS error
    FROM events
    WHERE type = 'tool'
      AND ($any_submission OR run_id IN (
              SELECT run_id FROM runs WHERE list_contains($submissions, submission_id)))
      AND ($any_tool OR json_extract_string(payload, '$.function') = $tool)
      AND ($any_agent OR agent_id = $agent)
      AND ($from_time IS NULL OR ts >= $from_time)
      AND ($to_time IS NULL OR ts <= $to_time)
    ORDER BY run_id, seq
    LIMIT $limit
"""


def search(
    store: ObjectStore,
    *,
    run_ids: Sequence[str] = (),
    submission_ids: Sequence[str] = (),
    tool: str = "",
    agent_id: str = "",
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[ToolCall], bool]:
    """The first `limit` matching calls by run, then seq, and whether more matched. Blocking."""
    con = query.connect(store, run_ids or None)
    try:
        con.execute(
            _SEARCH,
            {
                "result_chars": RESULT_CHARS,
                "any_submission": not submission_ids,
                "submissions": list(submission_ids) or [""],
                "any_tool": not tool,
                "tool": tool,
                "any_agent": not agent_id,
                "agent": agent_id,
                "from_time": from_time,
                "to_time": to_time,
                "limit": limit + 1,
            },
        )
        # Arrow, not Python rows: DuckDB needs `pytz` to build a Python datetime with a zone.
        rows = con.to_arrow_table().to_pylist()
    finally:
        con.close()
    return [ToolCall(**row) for row in rows[:limit]], len(rows) > limit
