# edge, CLI, and console

`edge` is the only public entry point. It authenticates callers, owns users and their
credentials, serves the console, and forwards to the internal services. The `swarm` CLI and the
web console are its two clients. Their place among the services is in
[architecture.md](../architecture.md). The plan they are built to is the
[M4 spec](../../spec/2026-10-03-m4-console/README.md).

**Status:** edge is built with authentication and run forwarding (M4 Plan step 2). Case calls,
analysis calls, the console, mTLS, the CLI, and deployment are not built yet. Items marked
*(proposed)* go beyond what the specs decided; they are listed under [Not settled](#not-settled).

## edge

Go, `go/cmd/edge` and `go/internal/edge`.

```bash
export SWARMEVAL_DATABASE_URL=postgresql://swarmeval:…@db:5432/swarmeval
edge user create root --admin            # the first admin; the password is read from stdin
edge serve --public-url https://swarm.example.com --tls-cert cert.pem --tls-key key.pem \
  --listen 0.0.0.0:7443 --control control:7090
```

| `serve` flag | Default | Meaning |
|---|---|---|
| `--public-url` | required | The address browsers use to reach edge. Its origin is the only one whose requests are accepted, and an https URL makes the session cookie `Secure` |
| `--listen` | `127.0.0.1:7443` | Without a certificate it must be a loopback address: edge refuses to serve plain HTTP to the network, so a deployment without `--tls-cert` puts a TLS-terminating proxy in front |
| `--tls-cert`, `--tls-key` | none | PEM certificate chain and key. Together or not at all |
| `--control` | required | The orchestrator's Control API, `host:port`, reached with gRPC over HTTP/2 without TLS until services use mTLS |
| `--session-idle` | `24h` | A browser session unused for this long ends |
| `--session-max-age` | `168h` | A browser session ends this long after sign-in, however active |

edge migrates its `tenant` schema when `serve` or `user create` starts.

- **Public API.** `swarmeval.api.v1` in [`proto/swarmeval/api/v1/`](../../proto/swarmeval/api/v1/),
  separate from the internal protos so that internal changes never break outside clients
  (M4 spec decision 1); `mise run proto:breaking` guards it. Defined so far: `AuthService`,
  `UserService`, `RunService`. Connect (connect-go) serves one definition as JSON to the
  browser and as gRPC to the CLI, over HTTP/1.1 or HTTP/2 (unencrypted HTTP/2 on a loopback
  listener). A request body may be up to 64 MiB of bundle plus encoding.
- **Run forwarding.** `RunService` calls the Control API's RPC of the same name and maps the run
  to the public `Run`, which leaves out the owner and lease fields. Each changing call carries the
  caller's username as `actor`, which the control plane records as `submitted_by`,
  `cancelled_by`, or `resumed_by` ([orchestrator.md](orchestrator.md#control-api)).
  `StreamEvents` is relayed message by message. A bundle over 64 MiB is refused before the
  control plane sees it.
- **Errors.** Control API errors that are about the caller's request (`INVALID_ARGUMENT`,
  `NOT_FOUND`, `FAILED_PRECONDITION`, `ALREADY_EXISTS`, `ABORTED`, `OUT_OF_RANGE`,
  `RESOURCE_EXHAUSTED`, `CANCELLED`, `DEADLINE_EXCEEDED`) go back with their message. Anything
  else is logged with its cause and returned as `UNAVAILABLE` with a generic message, so
  internal addresses and stack details do not leave the platform. edge's own failures, such as
  a database error, are `INTERNAL` with the step that failed, and the cause in the log.

### Authentication

Every RPC needs credentials except `AuthService.Login` and `LoginForToken`. There is no anonymous
access, the CLI included (M4 spec decision 3).

| Credential | Who | How |
|---|---|---|
| Session cookie | Browser | `Login` with a username and password sets an HttpOnly, `SameSite=Strict` cookie, `__Host-swarm_session` and `Secure` when the public URL is https, `swarm_session` otherwise. A session ends after `--session-idle` unused or `--session-max-age` after sign-in, on `Logout`, when the user is disabled, or when their password changes (except the session that changed it) |
| API token | CLI, scripts | `Authorization: Bearer swm_…`: `swm_` and 32 random bytes in base32. Made by `LoginForToken` (username and password, for `swarm login`) or `CreateToken` (signed in); shown once. Optional expiry; revocable. Refused while its user is disabled, and accepted again once they are enabled |

- **Storage.** Passwords are argon2id PHC strings (RFC 9106's second recommended parameters:
  64 MiB, 3 passes, 4 lanes), checked with the parameters stored in each hash. Sessions and
  tokens are stored only as the sha256 of their secret. `last_seen_at` and `last_used_at` are
  rewritten at most once a minute, so reads do not each become a write. Signing in deletes
  sessions that have ended.
- **CSRF.** A request whose `Origin` is not the public URL's origin is refused, sign-in
  included, and a request signed in by cookie must carry `Origin` at all. Browsers send
  `Origin` on every POST, and Connect's JSON calls need `Content-Type: application/json`,
  which a cross-site form cannot send. edge sends no CORS headers.
- **Sign-in throttling.** Failures are counted per username and per client address. After 5 in
  a row a key is locked for a minute, and each failure after that doubles the lock, up to 15
  minutes; a key with no failure for 15 minutes past its lock starts over, and a successful
  sign-in clears its username. A locked sign-in is `RESOURCE_EXHAUSTED` with the time left,
  even with the right password. Counts live in edge's memory: they start over when edge restarts
  and are not shared between replicas. Behind a proxy, every client has the proxy's address.
  `ChangePassword` checks the current password the same way.
- **What a refusal says.** A wrong password, an unknown user, and a disabled user get the same
  `UNAUTHENTICATED` message, and an unknown user is checked against a dummy hash so the three
  take as long. At most four argon2id computations run at once; more wait.
- **Roles.** `member` can do everything but manage users; `admin` can also create users,
  disable and enable them, and reset passwords (`UserService`). An admin cannot disable
  themselves.

### Authorization

Every authenticated caller sees every workspace (spec §9, question 8; M4 spec decision 5).
`workspace` is already a required field on cases, runs, and events, so per-workspace isolation
later means adding tables for workspaces and membership, a filter, and a check in edge, with no
data migration.

### Storage

The `tenant` schema in the shared Postgres, migrated by edge with goose from SQL files embedded
in the binary (`go/internal/edge/tenant/migrations/`). goose's version table is
`tenant.goose_db_version`, apart from Alembic's, and a Postgres session lock makes replicas
starting together migrate once. No other service reads the schema.

| Table | Holds |
|---|---|
| `users` | Username, argon2id hash, role, when disabled, created |
| `sessions` | sha256 of the session secret, user, created, last seen, expires |
| `api_tokens` | Public id (`tok_…`), sha256 of the token, user, name, created, expires, last used, revoked |

Users are disabled, never deleted, so the runs they submitted keep naming someone who existed.

### Not built yet

- **Forwarding** of case calls to the Control API and of analysis calls to `AnalysisService`
  (M4 Plan steps 4 and 5). The two sit side by side in the public API, and neither is nested
  under the other.
- **Internal mTLS** (step 7): edge will be the only caller the Control API and analysis accept.
- **Console assets**, embedded in the binary with `embed.FS` (step 6).

## swarm CLI

Not built (M4 Plan step 3). Go, with cobra. It ships as one static binary, so users need no
Python.

It is a client of edge and nothing more. There is no in-process path to the runtime:
`swarm run cases/x` packs the case directory and asks edge to submit it. The CLI imports no
`swarmeval` code and never imports `inspect_ai`. Its commands are in the M4 spec, decision 11.

## Console

Not built (M4 Plan step 6). React + Vite in the top-level `console/` project, with its own
README and changelog. It imports no `swarmeval` code. It calls edge through a client generated by
buf *(proposed: Connect-ES)*.

The replay viewer shows one lane per agent plus a lane for events no agent caused. Clicking an
event expands its causal chain through `parent_id`, events can be filtered by type and tag, and
two runs can be compared side by side.

## Tech choices

| Need | Choice | Why |
|---|---|---|
| Public API | `connectrpc.com/connect` | One proto definition serves JSON to browsers and gRPC to the CLI, with no hand-written gateway |
| Control API client | `connectrpc.com/connect` with its gRPC protocol | The same library as the public side; Go's `net/http` speaks unencrypted HTTP/2 itself, so no `golang.org/x/net` |
| Postgres (`tenant`) | `github.com/jackc/pgx/v5` | The standard maintained Go driver |
| `tenant` migrations | `github.com/pressly/goose/v3` | Plain SQL files embedded in the binary, a version table under the schema's name, and a Postgres session lock; each service owns its data, so edge's image needs no Python to migrate (M4 spec decision 14) |
| Password hashing | `golang.org/x/crypto/argon2` | Maintained by the Go team. The PHC string around it is ~50 lines of our own: the one wrapper library, `alexedwards/argon2id`, has had no release since 2023 |
| Command line | `github.com/spf13/cobra`, `golang.org/x/term` | `edge serve` and `edge user create`; the password prompt without echo |
| Tests | `github.com/testcontainers/testcontainers-go` | A throwaway Postgres, the image the Python tests use |
| Console | React + Vite | See [tech-stack.md](../tech-stack.md) |
| Browser client | `@connectrpc/connect-web` | Generated from the same protos as the Go side *(proposed)* |

## Not settled

1. Embedding the console in edge versus serving it separately (the M4 spec chooses embedding;
   not built).
2. Per-workspace authorization (spec §9, question 8).
3. Whether the actor of a cancel, resume, or fork should also go into the run's events (M4
   spec, Open question 1). Today it is recorded only on the control plane's rows.
4. Session lifetimes of 24 hours idle and 7 days at most (M4 spec, Open question 2).
