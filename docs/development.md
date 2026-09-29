# Development

## Prerequisites

- [mise](https://mise.jdx.dev/) — pins the uv version and runs the tasks below.
- Docker — needed once sandboxes land (M0). On Linux, gVisor (`runsc`) gives the default isolation
  level; see the spec's 运行平台 section.

uv installs Python 3.12 itself (pinned in `.python-version`), so a system Python is not needed.

## Setup

```bash
mise install
mise run sync
```

`sync` installs exactly what `uv.lock` records.

## Tasks

| Task | What it runs |
|---|---|
| `mise run check` | `lint`, `typecheck`, `test`. Must pass before a change is done |
| `mise run fmt` | `ruff format .` in place |
| `mise run lint` | `ruff format --check .` and `ruff check .` |
| `mise run typecheck` | `pyright` in strict mode over `swarmeval/` and `tests/` |
| `mise run test` | `pytest` (asyncio mode `auto`) |

## Dependencies

Add with `uv add <pkg>` (runtime) or `uv add --dev <pkg>` (tooling), and commit `uv.lock` with the
change. Check [AGENTS.md](../AGENTS.md) "Don't reinvent utilities" and the spec's 技术选型 table
before adding a new one.
