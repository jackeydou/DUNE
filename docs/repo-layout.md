# Repo layout

What is in the repo today, and where new code goes. Services and their boundaries are in
[architecture.md](architecture.md).

## Today

```text
AGENTS.md        standing orders for agents
CHANGELOG.md     what shipped in the Python package
README.md
docs/            how the system works now
spec/            one decision each, frozen once discussed
swarmeval/       the Python package: `core/` (case loading), `runtime/` (agent loop, extensions)
tests/           Python tests
go/              the Go module: `cmd/sandboxd`, `internal/`. Its own README, CHANGELOG, BUGFIX
proto/           gRPC contracts; buf.yaml and buf.gen.yaml sit at the repo root
pyproject.toml   uv.lock   mise.toml
```

There are two projects, each with its own ledgers at its root:

| Project | Root | Ledgers |
|---|---|---|
| Python package `swarmeval` | Repo root | `CHANGELOG.md`, `BUGFIX.md` at the repo root |
| Go module `github.com/jackeydou/DUNE/go` | `go/` | `go/CHANGELOG.md`, `go/BUGFIX.md` |

A change to `proto/` goes in the ledger of each project whose generated code it changes.

## Where new code goes

| Code | Place |
|---|---|
| `orchestrator`, `model-gateway`, `analysis` | Subpackages of `swarmeval/`: one uv project, one entry point per service. `openai` is imported only in `swarmeval/gateway/model/` |
| `edge`, `net-gateway`, `sandboxd`, the CLI | The Go module in `go/`: one `go/cmd/<name>` per binary, shared code under `go/internal/` |
| gRPC contracts | `proto/`, managed with buf. Go stubs are generated into `go/internal/gen/` and committed |
| Web console and replay | `console/` (M4) |
| docker compose and Helm chart | `deploy/` |
| Cases and suites | `cases/` and `suites/` |
