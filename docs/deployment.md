# Deployment

SwarmEval on one machine with docker compose: `deploy/compose/`. The services and how they are
isolated are in [architecture.md](architecture.md); k8s arrives in M5.

**Status:** the stack runs every service that is built: `edge`, the orchestrator's control plane
and one worker, `model-gateway`, `sandboxd`, Postgres, and RustFS. The `analysis` service and the
web console are not in it yet (M4 Plan steps 5 and 6); until then the public API is used through
the `swarm` CLI.

## What you need

- Linux with Docker Engine 26 or newer and the compose plugin 2.26 or newer (the stack mounts
  one directory of a volume per service, `volume.subpath`).
- gVisor (`runsc`) registered with docker, for the sandboxes: `runsc install`, then restart
  docker once, before SwarmEval is installed. Without it sandboxes run on runc and every run
  records that ([sandboxd](services/sandboxd.md)).
- The images your cases' sandboxes use, pulled on the host: sandboxd never pulls.
- A model backend with an OpenAI-compatible API, reachable from the host. It is not part of the
  stack.

## Start

```bash
cd deploy/compose
cp .env.example .env                    # fill in the three secrets
cp gateway.example.yaml gateway.yaml    # your backends and model names
docker compose up -d --build --wait
docker compose exec -T edge edge user create root --admin <<<'a long passphrase'
```

edge then serves `https://<SWARM_HOST>:<SWARM_PORT>` (default `https://localhost:7443`).

| `.env` | Default | Meaning |
|---|---|---|
| `POSTGRES_PASSWORD`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | required | Secrets of the stack's own Postgres and RustFS. Neither is published on the host |
| `SWARM_HOST`, `SWARM_PORT` | `localhost`, `7443` | The name or address, and the port, people use to reach edge. They form edge's `--public-url`, and the self-signed certificate names the host |
| `SWARM_BIND` | `0.0.0.0` | Host interface the port is published on; `127.0.0.1` keeps edge to the machine |
| `SWARM_TLS_CERT`, `SWARM_TLS_KEY` | the self-signed pair | Paths inside the edge container of the certificate browsers see. See [Certificates](#certificates) |
| `SWARM_STATE_DIR` | `/var/lib/swarmeval/sandboxd` | Where sandboxd keeps sandboxes' key paths. Mounted at the same path in the container, because the docker daemon mounts from it into sandboxes |
| `SWARM_SANDBOX_RUNTIME` | `auto` | `runsc`, `runc`, or `auto` |
| `SWARM_GATEWAY_CONFIG` | `./gateway.yaml` | The [model-gateway config](services/model-gateway.md). Backend keys go in `gateway.env`, one `NAME=value` per line, read by model-gateway only |
| `SWARM_WORKER_ID`, `SWARM_MAX_RUNS` | `worker-1`, `4` | The worker's id, which must stay the same across restarts, and how many runs it executes at once |
| `SWARM_ALLOW_CASE_CODE` | `0` | `1` accepts cases that load extensions from their own directory; that code runs in the worker ([orchestrator](services/orchestrator.md#case-code)) |
| `SWARM_VERSION` | `dev` | Tag of the two images, `swarmeval/python` and `swarmeval/go` |

## Using it

With a `swarm` built for your machine (`go build ./cmd/swarm` in `go/`):

```bash
docker compose exec -T edge cat /tls/tls.crt > swarm-edge.crt    # the self-signed certificate
swarm login --endpoint https://localhost:7443 --ca-file swarm-edge.crt
swarm run cases/scorer_misbelief --follow
```

Without one, the stack carries the CLI. It runs next to edge, reads the repository at `/work`
(`SWARM_CLI_DIR` mounts another directory), and keeps its sign-in in a volume:

```bash
docker compose run --rm cli login --username root
docker compose run --rm cli run cases/scorer_misbelief --follow
docker compose run --rm cli runs list
```

Commands: [edge.md](services/edge.md#swarm-cli).

## What runs

| Service | Image | Networks | Notes |
|---|---|---|---|
| `certs` | go | none | Runs `swarm-certs` at every `up`, then exits. Writes to the `certs` volume |
| `postgres` | `postgres:18-alpine` | platform | Volume `postgres` |
| `rustfs` | `rustfs/rustfs` | platform | Volume `objects` |
| `bucket` | python | platform | Creates the bucket if it is missing, then exits |
| `control` | python | platform | `swarmeval-control`; migrates the `control` and `runs` schemas at start |
| `model-gateway` | python | platform, egress | Reaches the model backend through `egress` |
| `sandboxd` | go | platform | Root, with the docker socket and the state directory |
| `worker` | python | platform, egress | `web_request` goes out through `egress` |
| `edge` | go | platform, public | The only published port; migrates the `tenant` schema at start |
| `cli` | go | edge's | Only with `docker compose run cli` |

- **Networks.** `platform` is an internal network: its containers reach each other and
  nothing else, so Postgres, RustFS, the control plane, and sandboxd have no route out and no
  port on the host. `egress` gives the worker and model-gateway a route out; `public` exists
  because docker publishes ports only for containers on a network that is not internal.
  Sandboxes are on no network at all ([architecture.md](architecture.md#isolation)).
- **model-gateway is on `egress`**, where the M4 spec put only the worker: the model backend is
  outside the stack, and the gateway is what calls it.
- **Service identity.** Every connection between services is mutual TLS
  ([architecture.md](architecture.md#service-identity)). Each service mounts its own directory
  of the `certs` volume and no other service's key. SwarmEval's own services other than
  sandboxd run as uid 10001.
- **sandboxd holds the docker socket**, which is root on the host. It accepts only the
  worker's certificate, and only `platform` reaches it.
- **Shared machines.** The stack does not restart the docker daemon, change iptables rules of
  its own, or remove anything it did not label: sandboxes carry SwarmEval's labels and
  are removed by the worker that ran them.

## Certificates

`certs` runs `swarm-certs` at every `docker compose up`:

- The **service CA and certificates** are written on first start and kept while they have more
  than 30 days left; a later `up` then replaces the certificates, under the same CA. Services
  read theirs at start, so follow a renewal with `docker compose restart`. To renew at once:
  `docker compose run --rm certs swarm-certs --out /certs --renew`.
- The **public certificate** is self-signed for `SWARM_HOST` and `127.0.0.1`, and is not signed
  by the service CA. People trust it by pinning it: `swarm login --ca-file`, or the browser's
  exception. It is kept while it names the same host and has more than 30 days left, so pins
  survive restarts.

To serve a certificate from a public CA instead, mount it into edge with a
`compose.override.yaml` and name the paths in `.env`:

```yaml
services:
  edge:
    volumes:
      - /etc/letsencrypt/live/swarm.example.com/fullchain.pem:/public/tls.crt:ro
      - /etc/letsencrypt/live/swarm.example.com/privkey.pem:/public/tls.key:ro
```

```bash
SWARM_TLS_CERT=/public/tls.crt
SWARM_TLS_KEY=/public/tls.key
```

The files must be readable by uid 10001.

## Operations

- **Data** is in the volumes `postgres` (runs, events, users, the case library's index) and
  `objects` (case bundles, exports, large blobs). Back both up together. `docker compose down`
  keeps them; `down --volumes` deletes them.
- **Upgrades.** Build or pull the new images and `docker compose up -d`. The control plane and
  edge migrate their schemas at start. Both images carry one version and are upgraded together.
- **Logs.** `docker compose logs -f worker control`. Refused connections between services are
  logged by the service that refused, with the caller's identity.
- **More workers.** Add a service that extends `worker` with its own `SWARM_WORKER_ID` in a
  `compose.override.yaml`. Do not use `--scale`: two workers with one id cannot run, and the
  second exits ([orchestrator](services/orchestrator.md#several-workers)).

## Smoke test

`deploy/compose/smoke.sh` (`mise run deploy:smoke`) is the M4 gate for what is built. It
brings the stack up under its own project name and port (17443) with a recorded model backend
(`smoke/backend.py` replays `smoke/recording.json`, one trajectory of
`cases/scorer_misbelief`), then, as a user would:

1. checks that the platform network is internal and that Postgres, RustFS, the control plane,
   and sandboxd are on no other;
2. creates the first admin;
3. with the CLI: signs in, pushes the case to the library, runs one variant with `--follow`,
   and reads the run's status, its four scores, and its events;
4. with the API as the console calls it (Connect JSON, session cookie, `Origin`): signs in,
   reads the run and the run list, and checks that a call without credentials is refused.

It calls no model, and pulls nothing at run time: `python:3.12-slim`, `busybox:latest`,
`postgres:18-alpine`, and `rustfs/rustfs:latest` must be on the host, and it says which is
missing. Building the two images fetches their base images and dependencies when they are not
cached. sandboxd's state goes in a directory the test makes under `/tmp` (`SMOKE_STATE_PARENT`
names another parent, for a docker daemon that does not share `/tmp`) and removes; it never
uses `SWARM_STATE_DIR` from the environment. It removes everything it made. `KEEP=1` leaves
the stack up. The console's half of the gate (create a case in the
browser, watch the replay) waits for the console.
