# edge, CLI, and console

`edge` is the only public entry point. It authenticates callers, owns users and their
credentials, serves the console, and forwards to the internal services. The `swarm` CLI and the
web console are its two clients. Their place among the services is in
[architecture.md](../architecture.md). The plan they are built to is the
[M4 spec](../../spec/2026-10-03-m4-console/README.md).

**Status:** built (M4 Plan steps 2 to 8): edge with authentication, run, case, and analysis
forwarding, the embedded console, and mutual TLS to the Control API and the analysis service;
the CLI with sign-in, runs, suites, events, forks, the case library, queries, reports,
exports, and `view`. All of it runs in the [compose deployment](../deployment.md). Items marked
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
| `--public-url` | required | The address browsers use to reach edge. Its origin is the only one whose requests are accepted, and an https URL makes the session cookie `Secure`. It is read in canonical form, as browsers send Origin: host in lower case, no default port |
| `--listen` | `127.0.0.1:7443` | Without a certificate it must be a loopback address: edge refuses to serve plain HTTP to the network, so a deployment without `--tls-cert` puts a TLS-terminating proxy in front |
| `--tls-cert`, `--tls-key` | none | PEM certificate chain and key. Together or not at all |
| `--control` | required | The orchestrator's Control API, `host:port`, reached with gRPC over HTTP/2: mutual TLS with `--mtls-cert`, plain text without |
| `--analysis` | none | The [analysis service](analysis.md#interface), `host:port`, reached the same way. Without it every `AnalysisService` call is `UNIMPLEMENTED`, with a message naming this flag |
| `--mtls-cert`, `--mtls-key`, `--mtls-ca` | none | edge's service certificate, its key, and the deployment's CA ([service identity](../architecture.md#service-identity)). All three or none. With them edge connects to the Control API and the analysis service as `edge`, and only to servers whose certificates name `control` and `analysis`. They are not `--tls-cert`, which is the certificate browsers see |
| `--session-idle` | `24h` | A browser session unused for this long ends |
| `--session-max-age` | `168h` | A browser session ends this long after sign-in, however active |

edge migrates its `tenant` schema when `serve` or `user create` starts.

- **Public API.** `swarmeval.api.v1` in [`proto/swarmeval/api/v1/`](../../proto/swarmeval/api/v1/),
  separate from the internal protos so that internal changes never break outside clients
  (M4 spec decision 1); `mise run proto:breaking` guards it. Defined so far: `AuthService`,
  `UserService`, `RunService`, `CaseService`, `AnalysisService`. Connect (connect-go) serves one definition as JSON to the
  browser and as gRPC to the CLI, over HTTP/1.1 or HTTP/2 (unencrypted HTTP/2 on a loopback
  listener).
- **Request limits.** A `RunService` or `CaseService` body may be up to 64 MiB of bundle plus
  encoding; an `AnalysisService` body 1 MiB (a rule set or a statement); `AuthService` and
  `UserService` bodies 64 KiB. A body must arrive within 30 seconds
  of the headers, 5 minutes for `SubmitRuns`, `SubmitSuite`, `PushCase`, and `UpdateCaseFiles`; the headers within 10 seconds. The deadline does not limit
  the answer, so an event stream lasts as long as the run. Idle keep-alive connections close after
  2 minutes.
- **Run forwarding.** `RunService` calls the Control API's RPC of the same name and maps the run
  to the public `Run`, which leaves out the owner and lease fields. `SubmitSuite` takes the suite
  file and one bundle per `cases[].path`; the control plane loads it, so edge and the CLI never
  parse the suite format ([orchestrator.md](orchestrator.md#suites)). Each changing call carries the
  caller's username as `actor`, which the control plane records as `submitted_by`,
  `cancelled_by`, or `resumed_by` ([orchestrator.md](orchestrator.md#control-api)).
  `StreamEvents` is relayed message by message, each event with its `line`. A bundle over 64 MiB,
  or a suite whose file and bundles pass 64 MiB in all, is refused before the control plane sees
  it.
- **Case forwarding.** `CaseService` is the [case library](orchestrator.md#case-library):
  `PushCase`, `UpdateCaseFiles`, `GetCase`, `ListCases`, `ListCaseRevisions`, `GetCaseRevision`,
  `ArchiveCase`, `UnarchiveCase`, each forwarded to the Control API's RPC of the same name. The
  writes carry the caller's username as `actor`; a revision returns it as `created_by`.
  `RunService.SubmitRuns` takes a bundle or `case` (workspace, case id, revision; 0 = newest),
  and `Run`, `SubmitRunsResponse`, and each `SuiteSubmission` name the `case_revision` used. An
  edit against a revision that is no longer the newest comes back `ABORTED` (HTTP 409 over
  Connect) with the newest revision's number in the message, which is how the console learns
  that someone else changed the case. A pushed bundle over 64 MiB, or an edit whose written
  files pass 64 MiB in all, is refused before the control plane sees it.
- **Analysis forwarding.** `AnalysisService` is the [analysis service](analysis.md#interface):
  `Query`, `SearchToolCalls`, `StartRuleScan`, `GetJob`, `Judge`, `Report`, `GetTrace`, and
  `DownloadExport`, each forwarded to the RPC of the same name. `Query` and `DownloadExport`
  are relayed chunk by chunk. `StartRuleScan` carries the caller's username as `actor`, and the
  job returns it as `created_by`. The public messages mirror the internal ones field for field
  with the same numbers; edge builds each request by hand, so a caller cannot set `actor`, and
  copies each response through the wire format (`TestAnalysisResponsesKeepEveryField` fails
  when the two drift apart).
- **Errors.** Control API and analysis service errors that are about the caller's request (`INVALID_ARGUMENT`,
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
  even with the right password. A username that cannot exist (not 1 to 64 of the allowed
  characters) is refused without a lookup and counts only against the address. Counts live in
  edge's memory: they start over when edge restarts
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

## swarm CLI

Go, with cobra: `go/cmd/swarm` and `go/internal/cli`. It ships as one static binary, so users
need no Python.

It is a client of edge and nothing more. There is no in-process path to the runtime:
`swarm run cases/x` packs the case directory and asks edge to submit it. The CLI imports no
`swarmeval` code and never imports `inspect_ai`. It speaks Connect's protocol, which works over
HTTP/1.1 and through any proxy, with the API token on every request.

```bash
swarm login --endpoint https://swarm.example.com     # asks username and password; saves a token
swarm run cases/collusion_pricing -V model=qwen3-8b,glm-5 --epochs 20 --follow
swarm run suites/m1_core.yaml
swarm runs list --suite m1_core.3fa1b2c4
swarm events collusion_pricing.fb47ae64.v0.e1        # one line per event, until the run ends
swarm case push cases/collusion_pricing -m "tighter threshold"
swarm run --case safety/collusion_pricing@3          # a revision in the library
swarm query "SELECT status, count(*) FROM runs GROUP BY ALL"
swarm report --suite m1_core.3fa1b2c4 --compare 'paraphrased=[],[dm_ab]'
swarm export collusion_pricing.fb47ae64.v0.e1       # the .eval, for `inspect view`
swarm replay RUN --fork-at EVENT --edit edits.yaml --follow
```

| Command | Does |
|---|---|
| `login` | Signs in with a username and password (asked on a terminal, otherwise the first line of stdin) and saves a new API token named `swarm CLI on <hostname>`; or `--token` saves one you have, after checking it. `--ca-file` saves a certificate to trust for edge |
| `logout` | Revokes the token `login` made and forgets it. A token given with `--token` is only forgotten |
| `whoami` | Who the token signs in as |
| `run CASE_DIR` | Packs the directory and submits it, with `-V axis=values` (repeatable), `--epochs`, and `--suite`. The directory is stored in the case library as `case push` stores it. Prints the submission, the revision its runs use, and the run ids |
| `run --case WORKSPACE/CASE[@REVISION]` | Submits a revision already in the library, the newest without `@REVISION`, with the same flags |
| `run SUITE_FILE` | Packs every case directory the suite's `cases[].path` names, relative to the file, and submits the suite whole (`SubmitSuite`). Prints each submission and the suite label |
| `runs list`, `get`, `cancel`, `resume` | `list` filters by `--submission`, `--case`, `--workspace`, `--status`, `--suite`, `--limit`. `get` also shows what a run reruns, what it was forked from and with what fidelity, and how often it was taken over from a worker whose lease ran out |
| `events RUN` | Prints `[event_id] #seq agent line` per event, as the judge reads them, until the run finishes; `--after SEQ` skips earlier ones |
| `replay RUN --fork-at EVENT` | `ForkRun`, with `--edit FILE`: a YAML or JSON list of edits in protobuf's JSON form (`replace_message`, `delete_message`, `replace_delivery`) |
| `case list` | The library's cases as `WORKSPACE/CASE`, each with its newest revision; `--workspace`, and `--archived` to include archived ones |
| `case push CASE_DIR` | Stores the directory as the next revision of the case its `case.yaml` names, `-m NOTE` for the revision list. Prints `WORKSPACE/CASE@N pushed`, or `unchanged, already` when the directory is the newest revision. Runs nothing |
| `case pull WORKSPACE/CASE[@REVISION] [DIR]` | Writes the revision's files, with their modes and links, to `DIR` (default `./CASE`), which must be empty or new |
| `case revisions WORKSPACE/CASE` | Revision, bundle hash, time, author, and note, newest first |
| `case archive`, `case unarchive` | An archived case leaves the list and takes no pushes, edits, or runs; nothing is deleted |
| `query SQL` | One read-only SELECT over the views `runs` and `events` ([analysis.md](analysis.md#queries)), as a table; `--csv`, or `--json` for one JSON object per row, where a column label that repeats (two columns of a join) gets `_2`, `_3`, … so every column is kept; `--max-rows N` (default and most 10,000). A result cut at the limit says so on stderr |
| `report` | The trigger rate report as Markdown, for `--submission ID` and `--suite LABEL` (both repeatable; neither means every run), with `--compare AXIS=A,B` for the difference between two values of an axis. `--json` prints the numbers |
| `export RUN` | Downloads the run's `.eval` (`--format eval`, the default) or events (`--format parquet`) to `RUN.eval` or `RUN.events.parquet`, or to `-o FILE` (`-` for stdout). It does not overwrite a file, and a download that fails, on the network or on the local disk, leaves none |
| `view RUN` | Prints the address of the run's page in the [console](#console) and opens it in a browser (`open` or `xdg-open`); `--no-open` only prints. The console asks for a sign-in of its own |
| `token create`, `list`, `revoke` | Your API tokens; `create --expires 720h` |
| `user create`, `list`, `disable`, `enable`, `reset-password` | Admins only. Passwords are asked twice on a terminal, read once from a pipe |

- **`--follow`** on `run` and `replay` streams each run's events as they happen, prefixed with
  the run id when there are several, at most 16 streams at a time. When every run has finished
  it prints their statuses and exits 1 if any did not end `done`. Ctrl-C stops following, not
  the runs.
- **`-V`** values are read as a YAML flow sequence, as `report --compare` reads them:
  `-V model=a,b` is `["a", "b"]`, `-V paraphrased=[],[dm_ab]` is `[[], ["dm_ab"]]`. The control
  plane checks them against the case.
- **Packing** follows `swarmeval.control.bundles.pack`: every regular file, sorted,
  `__pycache__` left out, times and owners zeroed, so the same files always give the same bytes.
  A symlink to a file goes up as the link, never as the file it points to; the control plane
  refuses a link that leaves the case directory or is absolute. Links to directories and links
  that point nowhere are left out. Over 64 MiB is refused before sending.
- **Pulling** writes regular files first and links last, and refuses any path that is not
  inside the directory, so nothing is ever written through a link. A pulled directory packs
  back to the same files.
- **Config.** `~/.config/swarm/config.yaml` (`$XDG_CONFIG_HOME/swarm/`, or `$SWARM_CONFIG`) holds
  `endpoint`, `token`, the id of a token `login` made, and `ca_file`. It is written mode 0600,
  through a rename. `$SWARM_ENDPOINT`, `$SWARM_TOKEN`, and `$SWARM_CA_FILE` override it, and
  `--endpoint` overrides both.
- **Self-signed edge.** `ca_file` is a PEM file trusted for edge besides the system's
  authorities: the certificate of an edge that serves a self-signed one
  ([deployment.md](../deployment.md#certificates)), or the CA that signed it. `login --ca-file`
  saves its absolute path. There is no switch that turns verification off.
- **Output.** Tables for people; `--json` prints records as protobuf JSON. Errors go to stderr as
  `swarm: <what>: <edge's message>`, with a hint for a missing sign-in or an unreachable edge, and
  exit 1.
- **Not built.** Rule scans, the judge, and traces are in the public API for the console and
  have no CLI command. Editing a case's files in place is the console's
  (`UpdateCaseFiles`); from the CLI, pull, edit, and push. `env up` and the
  `otel` / `docent` export formats wait for the network capability and the export adapters.

## Console

Built. A single-page app in the top-level [`console/`](../../console/README.md) project:
TypeScript, React, Vite, TanStack Router and Query, Tailwind with shadcn/ui components, and
CodeMirror 6 for the file editor. It imports no `swarmeval` code. It calls edge's public API
through a Connect-ES client generated by buf into `console/src/gen/` (committed, like the other
stubs), with Connect's JSON protocol and the session cookie.

- **Serving.** `mise run console:build` writes the build into `go/internal/edge/webui/static/`,
  which edge embeds (`embed.FS`); build edge after it. One binary then serves the pages and the
  API. A path that is a built file is served as it is; any other page path gets `index.html`,
  where the app's router takes over; a path under `/swarmeval.` never gets a page. Hashed
  assets are cached for good, `index.html` is revalidated on every load. An edge built without
  the console answers page requests with a 404 that says how to build it, and serves the API
  as usual.
- **No credentials for pages, credentials for data.** The pages are static and public; every
  call they make is an API call behind the authenticator. With no session the app shows the
  sign-in form and nothing else. Signing out reloads the app, so nothing the user read stays
  in memory. When any call comes back `UNAUTHENTICATED`, because the session ran out or was
  ended elsewhere, the app drops the session and shows the sign-in form at once, without a
  reload.
- **Headers.** Pages carry a Content-Security-Policy that allows scripts, fonts, and
  connections from edge's own origin only and forbids framing; styles may be inline, because
  the code editor writes its theme into a style element. Also `X-Content-Type-Options:
  nosniff` and `Referrer-Policy: no-referrer`.
- **Run content is text.** Model output, tool results, prompts, and injected payloads are
  rendered as text nodes, never as HTML, and no field is rendered as Markdown. Trajectories
  hold content that was built to attack whoever reads it.

| Page | Shows |
|---|---|
| Sign-in | Username and password |
| Runs | The newest 500 runs that match, grouped by submission; filtered by workspace, case, suite, submission, and status, all on the server, so a filter reaches runs that are not among the newest overall; the workspace list comes from the case library; refreshed every 5 seconds; each submission's trigger rates on demand (`Report`) |
| Run | Status, case revision, variant values, isolation, times, who submitted, cancelled, and resumed; cancel and resume; each scorer's last score, from the run's `score` events; the replay |
| Replay | The run's events from `StreamEvents`, in order, one lane per agent and one for events no agent caused, appended live while the run goes on. Types can be hidden. Clicking an event shows its stored payload and its causal chain (`GetTrace`, so only once the run is exported; a fork's chain goes on into its source), and, for a run that ended `done` or `cancelled`, forks from it with edits given as JSON |
| Compare | Two runs' replays side by side |
| Cases | The library by workspace; a new case from a template that loads and scores; a case's files in an editor (text files edited in place, binary files and links replaced by upload or deleted), saved as the next revision with a note; a save against a revision someone else has replaced says so and keeps the edits on screen; the revision history; the diff between two revisions, computed in the browser; submitting a revision with variant overrides and epochs; archive and unarchive |
| Analysis | SQL (`Query`, up to 1,000 rows), tool call search, rule scans (started as a job and polled), and the judge |
| Account | Change password; create, list, and revoke API tokens |
| Users | Admins: create, disable, enable, reset password |

Development: `pnpm dev` in `console/` serves the app on `http://127.0.0.1:5173` and proxies the
API to a local edge (`SWARM_EDGE`, default `http://127.0.0.1:7443`), which must run with
`--public-url http://127.0.0.1:5173`, the only origin it then accepts cookie requests from.

Not built: events carry no tags, so the replay filters by type only; the replay renders every
event of a run at once, which is slow past a few thousand events.

## Tech choices

| Need | Choice | Why |
|---|---|---|
| Public API | `connectrpc.com/connect` | One proto definition serves JSON to browsers and gRPC to the CLI, with no hand-written gateway |
| Control API client | `connectrpc.com/connect` with its gRPC protocol | The same library as the public side; Go's `net/http` speaks unencrypted HTTP/2 itself, so no `golang.org/x/net` |
| Postgres (`tenant`) | `github.com/jackc/pgx/v5` | The standard maintained Go driver |
| `tenant` migrations | `github.com/pressly/goose/v3` | Plain SQL files embedded in the binary, a version table under the schema's name, and a Postgres session lock; each service owns its data, so edge's image needs no Python to migrate (M4 spec decision 14) |
| Password hashing | `golang.org/x/crypto/argon2` | Maintained by the Go team. The PHC string around it is ~50 lines of our own: the one wrapper library, `alexedwards/argon2id`, has had no release since 2023 |
| Command line | `github.com/spf13/cobra`, `golang.org/x/term` | `edge serve` and `edge user create`, and the `swarm` CLI; the password prompt without echo |
| CLI config, suites, edit files | `go.yaml.in/yaml/v3` | The YAML project's maintained successor of `gopkg.in/yaml.v3`, which is archived; v4 is still a release candidate |
| Following many runs | `golang.org/x/sync/errgroup` | Bounded concurrent streams that stop together |
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
