# SwarmEval Go services

The Go module `github.com/jackeydou/DUNE/go`. It holds the services that talk to container
backends, the network, and the public: `sandboxd` and `edge` today, and later the `swarm` CLI.
`net-gateway` is deferred; its policy core is in `internal/netgw`.
How they fit with the rest of SwarmEval is in [docs/architecture.md](../docs/architecture.md).

## sandboxd

Creates sandboxes, runs tool calls in them, and reports what each call changed on disk and left
running. Behavior: [docs/services/sandboxd.md](../docs/services/sandboxd.md). API:
[`proto/swarmeval/sandbox/v1/sandbox.proto`](../proto/swarmeval/sandbox/v1/sandbox.proto).

```bash
go run ./cmd/sandboxd --state-dir /var/lib/swarmeval/sandboxd
```

| Flag | Default | Meaning |
|---|---|---|
| `--state-dir` | required | Key paths live under it. The docker daemon must see it at the same path; run sandboxd in a container with it bind-mounted at an identical path. Scratch: lost with the host |
| `--listen` | `127.0.0.1:7071` | gRPC address. No authentication, so bind it to the internal network only |
| `--runtime` | `auto` | `auto` (runsc when docker offers it, runc otherwise), `runc`, or `runsc` |
| `--sandbox-network` | `none` | `none`: sandboxes have only loopback. `per-sandbox`: a network per sandbox for net-gateway, which is not built, so a sandbox still reaches nothing ([docs](../docs/services/sandboxd.md#networks)) |
| `--sandbox-subnets` | `10.231.0.0/16` | With `per-sandbox`, the IPv4 pool that sandbox networks take a `/28` each from. Subnets the docker daemon already has are skipped |

Docker is found through the standard `DOCKER_HOST` / `DOCKER_*` environment.

Requirements and limits:

- Sandbox images must provide `sleep`, `tr`, and `/bin/sh`, `/etc/passwd` when the sandbox
  has users, and `/etc` when it has a machine id. sandboxd never pulls; pull images on the
  docker host first.
- In production sandboxd runs as root, so extracted key paths keep their owners.
- Sandboxes have no network but loopback by default. Agents reach the internet only through the
  worker's `web_request`.
- Output and content caps are in `sandboxd.DefaultConfig`: 64 KiB of stdout and stderr inline,
  16 MiB kept per stream, changed files sent back up to 1 MiB each and 64 MiB per call.
- State is in memory. A restarted sandboxd does not know the sandboxes it created; `DestroyRun`
  still removes their containers and networks by label.

## edge

The public entry point: authenticates callers and serves the public API
([`proto/swarmeval/api/v1`](../proto/swarmeval/api/v1/)), forwarding run calls to the
orchestrator's Control API. Behavior, flags, and limits:
[docs/services/edge.md](../docs/services/edge.md).

```bash
export SWARMEVAL_DATABASE_URL=postgresql://swarmeval:…@localhost:5432/swarmeval
go run ./cmd/edge user create root --admin      # password from stdin
go run ./cmd/edge serve --public-url http://127.0.0.1:7443 --control 127.0.0.1:7090
```

Requirements and limits:

- Postgres with the shared database; edge creates and migrates the `tenant` schema itself.
- Without `--tls-cert` / `--tls-key` it listens only on loopback.
- Sign-in throttling is kept in memory, per process.

## Layout

| Path | Holds |
|---|---|
| `cmd/sandboxd` | The binary: flags, docker connection, gRPC server |
| `cmd/edge` | The binary: `serve` and `user create` |
| `internal/edge` | The public API's handlers, authentication, sign-in throttling, and Control API forwarding |
| `internal/edge/tenant` | The `tenant` schema: migrations, users, sessions, API tokens, password hashing |
| `internal/sandboxd` | The service and its gRPC adapter |
| `internal/fsdiff` | Manifests of key paths and their diff |
| `internal/netgw` | net-gateway's policy engine, config, and traffic classification. Deferred: no binary uses it ([docs](../docs/services/net-gateway.md)) |
| `internal/driver` | The backend interface; `driver/docker` implements it |
| `internal/gen` | Stubs generated from `../proto` by `mise run proto:gen`. Do not edit |

## Development

From the repo root: `mise run check` lints and tests this module with everything else.
`mise run go:test-integration` runs sandboxd against the local docker daemon and needs
`busybox:latest` and `python:3.12-slim` on the host; it also runs edge against a throwaway
`postgres:18-alpine`. `SWARMEVAL_IT_RUNTIME=runc` or `runsc` picks the runtime. On macOS,
Docker Desktop and OrbStack enforce neither owners nor modes on bind mounts, so the `os_user`
permission test skips there; run it on Linux.
