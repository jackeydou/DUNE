# Changelog

## [Unreleased]

### Added
- Project skeleton: `swarmeval` package and the `mise run check` task
  (ruff, pyright strict, pytest).
- Agent runtime (`swarmeval.runtime`): a `round_robin` agent loop that records every model call
  and tool result before admitting it to an agent's context, with `max_turns` / `max_tokens`
  limits and sandbox and worker tools.
- Extension API (`swarmeval.runtime.extensions`): `@extension` setup functions that register
  hooks and tools, ten hook points, per-instance state, actions, and loading through the
  `swarmeval.extensions` entry point group. Every change an extension makes is recorded as an
  `intervention` event, and a failing extension fails the run.
- Case loading (`swarmeval.core`): `case.yaml` / `env.yaml` schema version 1 with `workspace`,
  private and shared sandboxes, channels, limits, and `extensions:`. `${variant.x}` expands into
  one variant per combination, every variant is validated at load, and `run_spec` maps a
  variant to the runtime's `RunSpec`. Format: `docs/case-format.md`.
- Postgres schema (`swarmeval.db`): `control.runs` and the `runs` tables `events`, `messages`,
  `agent_state`, and `extension_state`, created by Alembic migrations through `migrate(url)`.
- Event log (`swarmeval.events`): runtime records become Inspect events with SwarmEval fields
  under `metadata.swarmeval`, `inspect_ai` is pinned at 0.3.273, and each run's events form a
  SHA-256 hash chain over their RFC 8785 form, checked by `verify`.
- `PostgresRunStore`, the production `RunStore`: commits events, context messages, agent state,
  and extension state in one transaction, refuses writes from a stale `owner_epoch`, and sends
  `NOTIFY swarmeval_events` for each commit with events.
- `.eval` export (`swarmeval.events.export_run`): verifies a run's chain, rebuilds its events
  with the chain fields and expanded model input, groups them into one span per agent, and
  uploads `runs/<run_id>/sample.eval` to an S3-compatible bucket through `pyarrow.fs`.
- `ModelRequest.gen`: the context generation a request's messages come from, recorded as the
  model call's `gen` / `length`.
- `mise run test:docker` runs the tests that need a local docker daemon.
