# Changelog

## [Unreleased]

### Added
- `swarm login --ca-file`, config `ca_file`, and `$SWARM_CA_FILE`: a PEM file the CLI trusts
  for edge besides the system's authorities, for an edge with a self-signed certificate.
- `swarm-certs --public-host NAME`: also writes `public/{tls.crt,tls.key}`, a self-signed
  certificate for edge's `--tls-cert`, kept while it names the same hosts and has more than
  30 days left. `swarm-certs --renew` replaces every service certificate.
- `deploy/images/go.Dockerfile`: one image with `edge`, `sandboxd`, `swarm-certs`, and `swarm`.

### Changed
- `swarm-certs` keeps a service certificate that its CA signed, that names the same hosts, and
  that has more than 30 days left, where it replaced every certificate on every run. A
  deployment can now run it at every start.

### Added
- `swarm-certs`: writes a CA of the deployment's own and one certificate per service (`edge`,
  `control`, `worker`, `analysis`, `model-gateway`, `sandboxd`, `operator`), each naming its
  service in a URI SAN, `spiffe://swarmeval/<service>`. ECDSA P-256, one year; running it again
  keeps the CA and replaces the certificates, `--new-ca` replaces the CA, `--host
  SERVICE=NAME` adds a name a server is reached at. Each service's directory holds only that
  service's key.
- `sandboxd --mtls-cert --mtls-key --mtls-ca`: serves mutual TLS 1.3 and accepts only `worker`
  certificates; the handshake fails for any other caller, and the refusal is logged.
- `edge serve --mtls-cert --mtls-key --mtls-ca`: calls the Control API and the analysis
  service over mutual TLS as `edge`, and only servers whose certificates name `control` and
  `analysis`.

### Changed
- `sandboxd --listen` must be a loopback address unless sandboxd has a certificate; it exits
  otherwise. Breaking for a deployment that served sandboxd on a network address in plain text.

### Added
- Analysis. edge serves `swarmeval.api.v1.AnalysisService` (`Query`, `SearchToolCalls`,
  `StartRuleScan`, `GetJob`, `Judge`, `Report`, `GetTrace`, `DownloadExport`), forwarded to the
  analysis service named by `edge serve --analysis host:port`; without the flag the calls are
  `UNIMPLEMENTED`. `swarm query SQL` (table, `--csv`, or `--json`), `swarm report`
  (`--submission`, `--suite`, `--compare`), and `swarm export RUN` (`--format eval|parquet`).
- Case library. edge serves `swarmeval.api.v1.CaseService` (`PushCase`, `UpdateCaseFiles`,
  `GetCase`, `ListCases`, `ListCaseRevisions`, `GetCaseRevision`, `ArchiveCase`,
  `UnarchiveCase`), forwarded to the Control API with the caller as `actor`;
  `RunService.SubmitRuns` takes a bundle or a library revision (`case`), and runs and submit
  responses name their `case_revision`. `swarm case list|push|pull|revisions|archive|unarchive`,
  and `swarm run --case WORKSPACE/CASE[@REVISION]`. `swarm run CASE_DIR` prints the revision its
  runs use, and `swarm runs get` shows the case as `WORKSPACE/CASE@REVISION`.
- `swarm`, the command line: `login` (saves an API token), `logout`, `whoami`, `run` (a case
  directory with `-V` overrides and `--epochs`, or a suite file submitted whole), `--follow`
  (streams every run's events, then their statuses; exits 1 unless all ended `done`), `runs
  list|get|cancel|resume`, `events`, `replay --fork-at --edit`, `token`, and `user`. A client of
  edge over Connect's protocol; config in `~/.config/swarm/config.yaml`, mode 0600. A case is
  packed as Python's `pack` packs it, symlinks as links.
- edge forwards `RunService.SubmitSuite` and each streamed event's `line`.
- `edge`: the public entry point. `edge serve` serves `swarmeval.api.v1` with connect-go:
  `AuthService` (password sign-in to an HttpOnly, SameSite=Strict session cookie; API tokens
  `swm_…` for the CLI; change password), `UserService` (admins create, disable, and reset users),
  and `RunService`, forwarded to the Control API with the caller as `actor`. Every RPC but
  sign-in needs credentials; cookie requests must come from the public URL's origin; sign-ins
  are throttled per username and address. Request bodies are bounded in size and in time.
  TLS with `--tls-cert`, or loopback only.
  `edge user create` makes the first admin. The `tenant` schema (users, sessions, tokens) is
  migrated by edge with goose. Passwords are argon2id.
- `swarmeval.api.v1` stubs, the public API: `AuthService`, `UserService`, and `RunService`.
  connect-go handlers and clients are now generated for every proto package, next to the
  grpc-go stubs.
- `swarmeval.control.v1` stubs: `actor` on `SubmitRunsRequest`, `CancelRunRequest`,
  `ResumeRunRequest`, and `ForkRunRequest`; `Run.submitted_by`, `cancelled_by`, `resumed_by`.
- `FsChange.mtime_ns`: every reported change carries the path's modification time.
- `RestoreFiles`: removes paths, creates directories, and writes files in a sandbox's key paths
  before its first `Exec`, then retakes the manifest, so a fork's restored files are its
  baseline. `ErrState` maps to `FAILED_PRECONDITION`; `Config.RestoreLimit` (3 MiB) caps one
  request. With Go and Python stubs.
- `swarmeval.control.v1` stubs: `ForkRun` and its edits; `Run.forked_from`, `fork_seq`,
  `fidelity`.
- `swarmeval.control.v1` stubs: `ResumeRun`, with `ResumeRunRequest` and `ResumeRunResponse`.
- `swarmeval.control.v1` stubs: `Run.replaces`, the interrupted run a rerun stands in for;
  `suite` on `SubmitRunsRequest`, `Run`, and `ListRunsRequest`.
- `CreateSandbox` takes the sandbox's identity: `env` (every process's environment, exec'd
  commands included), `hostname`, and `machine_id`, written to `/etc/machine-id` before the
  container starts. Each is validated; a key path that would hide `/etc/machine-id` is refused
  with a machine id. The driver's `ContainerSpec` takes `Env` and `Hostname`. The worker uses
  them for per-sandbox canaries.
- `internal/netgw`, net-gateway's platform-independent core: first-hit policy (`allow`, `deny`,
  `log_and_deny`), the config file, and TLS SNI and HTTP request-line classification, with Go and
  Python stubs for `swarmeval.netgw.v1.NetEventsService`. No binary uses either: net-gateway is
  deferred (2026-10-02, `docs/services/net-gateway.md`).
- `CreateRun` creates one network per sandbox before its sandboxes: a bridge with no host address
  and no masquerading, on a `/28` from `--sandbox-subnets` (default `10.231.0.0/16`) that skips
  every subnet docker already has. `DestroyRun` removes the run's networks after its containers.
- Sandboxes join their network, with `/etc/resolv.conf` bind-mounted to name the gateway and the
  gateway as docker's DNS server, so docker's embedded resolver under runc forwards nowhere else.
- `CreateSandbox` takes `users`: each is added to the image's `/etc/passwd` and `/etc/group` with a
  free id from 1000 and a `0700` home under `/home`, before the container starts.
- `sandboxd` with a docker driver on runc: `CreateSandbox`, `Exec`, `ReadFile`, `FinalDiff`, and
  `DestroyRun` of `swarmeval.sandbox.v1.SandboxService`. Sandboxes are offline, drop every
  capability, and get CPU, memory, pids, and disk limits. Key paths start with the image's
  content and are bind-mounted from a host state directory.
- File diff and process snapshot around every call: `fs` changes with before and after hashes,
  owner, and protected flag, content for small files, background changes marked ambiguous with
  the calls whose processes were alive, and processes a call left running.
- Timeouts kill the command and its descendants inside the sandbox, as the call's user.
- Generated stubs for `swarmeval.modelgw.v1.RecorderService`. No Go code uses them yet.
- `CreateSandbox` takes seed files: written into key paths through `os.Root` after the image
  content and before the first manifest, so they are part of the baseline. At most 1 MiB in all.

### Changed
- Sandboxes have no network by default: `--sandbox-network none` runs them with docker's
  `--network none`, so they have only loopback and a connection out fails at once. `CreateRun`
  registers the run and creates networks only with `--sandbox-network per-sandbox`, which keeps
  the per-sandbox networks for net-gateway. `--sandbox-subnets` applies only there.
- `CreateSandbox` needs the run from `CreateRun`. A sandbox not listed there is refused.
- `--runtime` defaults to `auto`: gVisor where docker offers it.
- Processes are listed by a script inside the sandbox that reads its `/proc`, not by `docker top`.
  `Process.pid` is now the pid inside the sandbox, and `user` comes from the sandbox's
  `/etc/passwd`. Images must provide `tr`. A listing over 4 MiB fails the call.
- The driver's `Processes` is gone; `CreateNetwork`, `Subnets`, `ListNetworksByLabels`, and
  `RemoveNetwork` are new, and `ContainerSpec` takes `Network` and `Files`.

### Fixed
- A key path's own directory keeps the image's mode and owner, and setuid, setgid, and sticky bits
  survive extraction. See `BUGFIX.md`.
- Under runsc, processes a call left running are reported. See `BUGFIX.md`.
