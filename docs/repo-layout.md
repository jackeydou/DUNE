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
swarmeval/       the Python package: `core/` (case loading), `runtime/` (agent loop, extensions),
                 `db/` (Postgres tables, migrations), `events/` (Inspect events, hash chain, run store),
                 `sandbox/` (sandboxd client, blob store), `gateway/` (model-gateway, Message Bus),
                 `honeypot/` (canaries), `scorers/`, `control/` (Control API, queue),
                 `worker/` (run lifecycle), `web/` (`web_request`'s client), `detect/` (detectors),
                 `monitor/` (the online Monitor), `analysis/` (batch jobs over exports), `proto/`
                 (generated gRPC stubs), `mtls.py` (service certificates and who may call whom)
tests/           Python tests
cases/           cases, one directory each: `scorer_misbelief/`, `collusion_pricing/`
suites/          suites, one file each: `m1_core.yaml`
go/              the Go module: `cmd/` (`sandboxd`, `edge`, `swarm`, `swarm-certs`), `internal/`. Its own README, CHANGELOG, BUGFIX
console/         the web console: a Vite + React app, built into edge. Its own README, CHANGELOG
proto/           gRPC contracts; buf.yaml and buf.gen.yaml sit at the repo root
deploy/          `images/` (one Dockerfile for the Python services, one for the Go binaries) and
                 `compose/` (the single-machine stack and its smoke test)
pyproject.toml   uv.lock   mise.toml
```

There are three projects, each with its own ledgers at its root:

| Project | Root | Ledgers |
|---|---|---|
| Python package `swarmeval` | Repo root | `CHANGELOG.md`, `BUGFIX.md` at the repo root |
| Go module `github.com/jackeydou/DUNE/go` | `go/` | `go/CHANGELOG.md`, `go/BUGFIX.md` |
| Web console | `console/` | `console/CHANGELOG.md`, `console/BUGFIX.md` |

A change to `proto/` goes in the ledger of each project whose generated code it changes.

## Where new code goes

| Code | Place |
|---|---|
| `orchestrator`, `model-gateway`, `analysis` | Subpackages of `swarmeval/`: one uv project, one entry point per service. `openai` is imported only in `swarmeval/gateway/model/` |
| `edge`, `net-gateway`, `sandboxd`, the CLI | The Go module in `go/`: one `go/cmd/<name>` per binary, shared code under `go/internal/` |
| gRPC contracts | `proto/`, managed with buf. Go stubs are generated into `go/internal/gen/`, Python stubs into `swarmeval/proto/`, and the console's client of the public API into `console/src/gen/`; all are committed |
| Web console and replay | `console/`: pages in `src/pages/`, shared pieces in `src/components/`, shadcn/ui components in `src/components/ui/` and their hooks in `src/hooks/` (added with the shadcn CLI, not hand-written), pure helpers with their tests in `src/lib/`, Playwright specs in `e2e/` |
| docker compose and Helm chart | `deploy/` |
| Cases and suites | `cases/` and `suites/` |
