# SwarmEval Go services

The Go module `github.com/jackeydou/DUNE/go`. It holds the services that talk to container
backends and the network: `sandboxd` today, and later `net-gateway`, `edge`, and the `swarm` CLI.
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
| `--sandbox-subnets` | `10.231.0.0/16` | IPv4 pool that sandbox networks take a `/28` each from. Subnets the docker daemon already has are skipped |

Docker is found through the standard `DOCKER_HOST` / `DOCKER_*` environment.

Requirements and limits:

- Sandbox images must provide `sleep`, `tr`, and `/bin/sh`, and `/etc/passwd` when the sandbox
  has users. sandboxd never pulls; pull images on the docker host first.
- In production sandboxd runs as root, so extracted key paths keep their owners.
- Each sandbox has its own network, on which the host has no address. Until net-gateway exists,
  nothing holds the gateway address, so a sandbox reaches nothing.
- Output and content caps are in `sandboxd.DefaultConfig`: 64 KiB of stdout and stderr inline,
  16 MiB kept per stream, changed files sent back up to 1 MiB each and 64 MiB per call.
- State is in memory. A restarted sandboxd does not know the sandboxes it created; `DestroyRun`
  still removes their containers and networks by label.

## Layout

| Path | Holds |
|---|---|
| `cmd/sandboxd` | The binary: flags, docker connection, gRPC server |
| `internal/sandboxd` | The service and its gRPC adapter |
| `internal/fsdiff` | Manifests of key paths and their diff |
| `internal/driver` | The backend interface; `driver/docker` implements it |
| `internal/gen` | Stubs generated from `../proto` by `mise run proto:gen`. Do not edit |

## Development

From the repo root: `mise run check` lints and tests this module with everything else.
`mise run go:test-integration` runs sandboxd against the local docker daemon and needs
`busybox:latest` on the host. `SWARMEVAL_IT_RUNTIME=runc` or `runsc` picks the runtime. On macOS,
Docker Desktop and OrbStack enforce neither owners nor modes on bind mounts, so the `os_user`
permission test skips there; run it on Linux.
