# Development

## Prerequisites

- [mise](https://mise.jdx.dev/) — pins uv, Go, golangci-lint, buf, and the protoc plugins, and
  runs the tasks below.
- Docker — for the tests that start containers (`test:docker`, `go:test-integration`) and for
  running sandboxes. `mise run check` does not need it. On Linux, gVisor (`runsc`) gives the default isolation level from M1; see the spec's
  运行平台 section.

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
| `mise run check` | `lint`, `typecheck`, `test`, `go:lint`, `go:test`, `proto:lint`. Must pass before a change is done |
| `mise run fmt` | `ruff format`, `golangci-lint fmt`, and `buf format` in place |
| `mise run lint` | `ruff format --check .` and `ruff check .` |
| `mise run typecheck` | `pyright` in strict mode over `swarmeval/` and `tests/` |
| `mise run test` | `pytest` (asyncio mode `auto`), without tests marked `docker` |
| `mise run test:docker` | `pytest -m docker`: the Postgres store, migrations, and export against a throwaway `postgres:18-alpine` and `rustfs/rustfs` from testcontainers, and the sandboxd client against a sandboxd it builds from `go/` (needs `busybox:latest`). Run it before a change to `swarmeval/db/`, `swarmeval/events/`, or `swarmeval/sandbox/` is done |
| `mise run go:lint` | `golangci-lint run` over `go/`, integration tests included |
| `mise run go:test` | `go test ./...` in `go/` |
| `mise run go:test-integration` | sandboxd against the local docker daemon. Needs `busybox:latest` |
| `mise run proto:gen` | Go stubs into `go/internal/gen/` with `buf generate`, and Python stubs with typed `.pyi` into `swarmeval/proto/` with grpcio-tools and mypy-protobuf. Commit both |
| `mise run proto:lint` | `buf lint`, `buf format --diff`, and a check that the committed stubs match `proto/` |

## Dependencies

Python: `uv add <pkg>` (runtime) or `uv add --dev <pkg>` (tooling), and commit `uv.lock` with the
change. Go: `go get` inside `go/`, then `go mod tidy`, and commit `go.mod` and `go.sum`. Check [AGENTS.md](../AGENTS.md) "Don't reinvent utilities" and
[tech-stack.md](tech-stack.md) before adding a new one.
