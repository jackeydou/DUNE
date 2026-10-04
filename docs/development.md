# Development

## Prerequisites

- [mise](https://mise.jdx.dev/) — pins uv, Go, golangci-lint, buf, and the protoc plugins, and
  runs the tasks below.
- Docker — for the tests that start containers (`test:docker`, `go:test-integration`) and for
  running sandboxes. `mise run check` does not need it. On Linux, install gVisor (`runsc`) and
  register it with docker (`runsc install`): sandboxd then uses it by default, and records `runc`
  as the run's isolation where it is missing. On macOS, Docker Desktop and OrbStack offer only
  runc and ignore file owners and modes on bind mounts, so `os_user` boundaries do not hold there;
  check anything that depends on them on Linux.

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
| `mise run check` | `lint`, `typecheck`, `test`, `go:lint`, `go:test`, `proto:lint`, `proto:breaking`. Must pass before a change is done |
| `mise run fmt` | `ruff format`, `golangci-lint fmt`, and `buf format` in place |
| `mise run lint` | `ruff format --check .` and `ruff check .` |
| `mise run typecheck` | `pyright` in strict mode over `swarmeval/` and `tests/` |
| `mise run test` | `pytest` (asyncio mode `auto`), without tests marked `docker` |
| `mise run test:docker` | `pytest -m docker`: the Postgres store, migrations, and export against a throwaway `postgres:18-alpine` and `rustfs/rustfs` from testcontainers, and the sandboxd client against a sandboxd it builds from `go/` (needs `busybox:latest`). Run it before a change to `swarmeval/db/`, `swarmeval/events/`, or `swarmeval/sandbox/` is done |
| `mise run go:lint` | `golangci-lint run` over `go/`, integration tests included |
| `mise run go:test` | `go test ./...` in `go/` |
| `mise run go:test-integration` | sandboxd against the local docker daemon. Needs `busybox:latest` |
| `mise run proto:gen` | Go stubs (grpc-go and connect-go) into `go/internal/gen/` with `buf generate`, and Python stubs with typed `.pyi` into `swarmeval/proto/` with grpcio-tools and mypy-protobuf. Commit both. The public API (`proto/swarmeval/api/`) gets no Python stubs: only edge and its clients use it |
| `mise run proto:lint` | `buf lint`, `buf format --diff`, and a check that the committed stubs match `proto/` |
| `mise run proto:breaking` | `buf breaking` against the local `main` branch. Only the public API, `proto/swarmeval/api/`, is checked; `buf.yaml` lists the internal packages it ignores, and a new internal package goes on that list |

## Running a case

Every service on one machine, against a real model. Nothing here is needed for `mise run check`.

```bash
# Postgres and an S3-compatible store.
docker run -d --name swarm-pg -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres:18-alpine
docker run -d --name swarm-s3 -e RUSTFS_ACCESS_KEY=dev-key -e RUSTFS_SECRET_KEY=dev-secret \
  -p 9000:9000 rustfs/rustfs:latest
export SWARMEVAL_DATABASE_URL=postgresql://postgres:dev@127.0.0.1:5432/postgres
export SWARMEVAL_S3_ACCESS_KEY=dev-key SWARMEVAL_S3_SECRET_KEY=dev-secret
uv run python -c "from pyarrow.fs import S3FileSystem as S; S(access_key='dev-key', \
  secret_key='dev-secret', endpoint_override='127.0.0.1:9000', scheme='http', \
  allow_bucket_creation=True).create_dir('swarmeval')"

# sandboxd. The docker daemon must see the state directory at the same path.
(cd go && go run ./cmd/sandboxd --state-dir "$PWD/../.state/sandboxd") &

# model-gateway, with a config naming the case's model (see docs/services/model-gateway.md).
uv run swarmeval-model-gateway --config gateway.yaml &

# Control plane (migrates the database) and one worker.
S3="--s3-endpoint 127.0.0.1:9000 --s3-scheme http --s3-bucket swarmeval"
uv run swarmeval-control $S3 &
uv run swarmeval-worker $S3 --worker-id dev &
```

Submit through the Control API with [grpcurl](https://github.com/fullstorydev/grpcurl). The bundle
is the case directory as a tar archive, base64-encoded in JSON:

```bash
# COPYFILE_DISABLE keeps macOS tar from adding ._ metadata files.
jq -n --arg bundle "$(COPYFILE_DISABLE=1 tar -C cases/scorer_misbelief -cf - . | base64)" \
  '{case_bundle: $bundle, overrides: {model: ["qwen3-8b"]}}' |
grpcurl -plaintext -import-path proto -proto swarmeval/control/v1/control.proto -d @ \
  127.0.0.1:7090 swarmeval.control.v1.ControlService/SubmitRuns
```

`GetRun`, `ListRuns`, `CancelRun`, and `StreamEvents` take the run ids it returns.

A case that loads extensions from its own directory (`case:`, such as `cases/collusion_pricing`)
needs both roles started with `--allow-case-code`; its code then runs inside the worker
([orchestrator.md](services/orchestrator.md#case-code)).

A [suite](case-format.md#suites) submits a set of cases over a model matrix. Edit its `models:`
to names the gateway serves, check it, and submit it; it prints the label to report on:

```bash
uv run python -m swarmeval.control.suite check suites/m1_core.yaml
uv run python -m swarmeval.control.suite submit suites/m1_core.yaml --control 127.0.0.1:7090
uv run python -m swarmeval.analysis report --suite m1_core.<hex> $S3
```

More workers run more runs at once. Start each with its own `--worker-id`; a second worker with
an id already in use exits. Restart a worker with the same id, so the runs it left unfinished are
marked `interrupted` and rerun:

```bash
uv run swarmeval-worker $S3 --worker-id dev2 --max-runs 2 &
``` A finished
run's log is `runs/<run_id>/sample.eval` in the bucket; download it and open it with
`uv run inspect view`.

## Dependencies

Python: `uv add <pkg>` (runtime) or `uv add --dev <pkg>` (tooling), and commit `uv.lock` with the
change. Go: `go get` inside `go/`, then `go mod tidy`, and commit `go.mod` and `go.sum`. Check [AGENTS.md](../AGENTS.md) "Don't reinvent utilities" and
[tech-stack.md](tech-stack.md) before adding a new one.
