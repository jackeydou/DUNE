# Repo layout

What is in the repo today, and where new code goes. Services and their boundaries are in
[architecture.md](architecture.md).

## Today

```text
AGENTS.md        standing orders for agents
CHANGELOG.md     what shipped (the Python package's changelog)
README.md
docs/            how the system works now
spec/            one decision each, frozen once discussed
swarmeval/       the Python package: `core/` (case loading), `runtime/` (agent loop, extensions)
tests/
pyproject.toml   uv.lock   mise.toml
```

The Python package is the only project so far. Its root is the repo root, which is why its
`CHANGELOG.md` sits there.

## Where new code goes

| Code | Place |
|---|---|
| `orchestrator`, `model-gateway`, `analysis` | Subpackages of `swarmeval/`: one uv project, one entry point per service. `openai` is imported only in `swarmeval/gateway/model/` |
| `edge`, `net-gateway`, `sandboxd`, the CLI | One Go module, one `cmd/<name>` per binary, shared code under `internal/` |
| gRPC contracts | `proto/`, managed with buf |
| Web console and replay | `console/` (M4) |
| docker compose and Helm chart | `deploy/` |
| Cases and suites | `cases/` and `suites/` |

Two things are not decided yet. Ask the user before creating the first Go file:

- Where the Go module lives: its own directory, or `go.mod` at the repo root.
- Whether the Go module gets its own `CHANGELOG.md` and `BUGFIX.md`, or shares the root ones.
  All services release under one version.
