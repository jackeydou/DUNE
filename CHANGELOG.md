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
