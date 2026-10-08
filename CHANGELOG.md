# Changelog

## [Unreleased]

### Fixed
- Leaving a `GatewaySession` returns once model-gateway has detached the run, so no call
  made after it reaches the backend.

### Added
- `cases/boss_email_link`: one agent uses only `computer` screenshots and desktop input to
  handle a boss's email. Ordinary phishing, urgent phishing, and legitimate-link variants
  run against sandbox-local mock pages; scores record phishing visits, visits before
  observable link/sender checks, and access to the trusted project brief.
- `browser` and `computer`: agents drive a web browser and a virtual screen inside their sandbox
  (`spec/2026-10-07-browser-computer-use`). A profile's `display` (`env.yaml` schema version 2)
  starts the display image's screen and Chromium; `browser` acts on accessibility-snapshot refs,
  `computer` on screen pixels, both run as the display's own user, and their screenshots reach
  the model as images. The sandbox keeps no network: the browser reaches loopback and files only.
  No agent may set `os_user: swarmdisplay`, the display's own user.
  `deploy/images/display` builds the image (`mise run display:build`).
- Tool results may carry images (`ToolResult.images`, `ToolMessage.images`, `ImageRef`), stored
  as blobs and referenced by hash. `RequestOptions.max_images` (default 3) keeps a request to
  the context's latest images; `before_model_request` may change it.
- model-gateway accepts `image_url` parts (inline PNG `data:` URLs) in tool messages and moves
  them into a user message after the tool messages before sending upstream, after reasoning
  passback.
- `Exec.user` and `Exec.collect`: a sandbox tool may run as another user and have sandboxd read
  files out of the sandbox after the call. `SandboxTool.output` turns sandboxd's result into the
  agent's.
- The `.eval` export embeds a run's images: tool results inline, contexts as attachments, with
  `[image omitted]` where a request left one out.

### Changed
- Event schema version 10: tool events carry `images`, model events `max_images`, `exec`
  observations `collected`. Older runs read as before.
- `env.yaml` schema version 2 adds a profile's `display`. Version 1 files load as before.
- A fork at a turn after a run's first `browser` or `computer` call is refused
  (`FAILED_PRECONDITION`): a fork restores files, not a browser.
- `GatewaySession` takes `images`, where it reads the images requests carry;
  `check_transcript` takes `images` to rebuild them.
- Models are chosen when a case is submitted, not written in the case
  (`spec/2026-10-06-run-time-models`). `case.yaml` schema version 4: an agent names a
  `model_slot` (default `default`) instead of a `model`, and agents in one slot run one model.
  `SubmitRuns` takes `models`, slot → model names, required for every slot of the case; each
  slot's models are one more dimension of the run matrix, and a run's `task_args` name them as
  `model.<slot>`. Breaking: cases of schema versions 1 to 3 no longer load (the error says how
  to move them), and library revisions in those versions cannot run. `cases/` is moved;
  `cases/collusion_pricing`'s paraphraser is fixed at `minimax-m3`.
- Suite schema version 2: `models` fills model slots, as a list (each case's `default` slot) or
  a mapping by slot, and an entry's own `models` replaces the suite's per slot. Breaking: version
  1 suites no longer load.
- Event schema version 9: a fork's `intervention` may be `replace_model`. Older runs read as
  before.

### Added
- An agent can run without a sandbox (`spec/2026-10-07-optional-sandbox`). `case.yaml` schema
  version 5: `sandbox: none` gives an agent no sandbox; it cannot list `shell` or set
  `os_user`. A case where no agent has one needs no `env.yaml`, and its runs never call
  sandboxd: no sandboxes, isolation self-check, final diff, or teardown, and `isolation` stays
  empty. In a case where only some agents have one, only those sandboxes are created and
  checked. `cross_sandbox` counts an agent without a sandbox as outside every sandbox. Version 4
  cases load as before, `none` included as an instance name. `cases/collusion_pricing` moves to
  version 5: its sellers have no sandbox and the case has no `env.yaml`.
- CI: `mise run check` on every pull request and push to `main`
  (`.github/workflows/check.yml`).
- The control plane asks model-gateway which models it serves (`swarmeval-control
  --gateway-http`) and refuses a submission or fork naming another, before anything is stored;
  model-gateway out of reach is `UNAVAILABLE`. `ControlService.ListModels` lists them.
  model-gateway accepts the `control` certificate for `GET /v1/models` only. The compose stack
  points the control plane at model-gateway.
- `control.run_specs.models` (migration 0013): each run's model per slot.
- `ForkEdit.replace_model`: a fork may run a model slot on another model from the fork point;
  forks of that fork keep it.
- `GetCaseRevision` returns the revision's `model_slots`, or `load_error` for a revision that no
  longer loads.

### Added
- `Run.takeovers` (migration 0012, `control.runs.takeovers`): how many times a worker claimed
  the run after its owner's lease ran out. The Control API returns it.
- `swarmeval-worker --stay-halted`: a worker that a failed isolation self-check halted stays
  up without claiming runs instead of exiting, so a restart policy does not put it back on
  the broken host. The compose stack sets it.

### Changed
- `swarmeval-worker`, `swarmeval-control`, and `swarmeval-analysis` stop on SIGTERM as on
  Ctrl-C and exit 0: the
  worker cancels its runs, which removes their sandboxes. Before, a worker that was a
  container's first process ignored SIGTERM, was killed when the grace period ended, and left
  its sandboxes until a worker with its id started again.
- compose: the worker has a 1 minute `stop_grace_period`.
- compose: `control` and `analysis` are healthy once they listen, and `docker compose up
  --wait`, edge, the worker, and analysis wait for that instead of for the container to start.

### Added
- Single-machine deployment, `deploy/compose/`: `docker compose up` runs edge, the control
  plane, a worker, the analysis service, model-gateway, sandboxd, Postgres, and RustFS, with mutual TLS between the
  services from certificates generated at start, platform services on a network with no route
  out, and edge the only published port, on https. `deploy/images/python.Dockerfile` is the
  image of the Python services. `deploy/compose/smoke.sh` (`mise run deploy:smoke`) runs
  `cases/scorer_misbelief` through the stack with a recorded model backend. Setup and
  operations: `docs/deployment.md`.
- Mutual TLS between services (`swarmeval.mtls`). `swarmeval-control`, `swarmeval-worker`,
  `swarmeval-model-gateway`, `swarmeval-analysis`, `python -m swarmeval.control.suite submit`,
  and `python -m swarmeval.analysis judge` take `--mtls-cert`, `--mtls-key`, and `--mtls-ca`,
  the files `swarm-certs` writes. With them the Control API accepts only `edge` and `operator`
  certificates, the analysis service only `edge`, and model-gateway (HTTP and
  `RecorderService`) only `worker` and `analysis`:
  another service's call is `PERMISSION_DENIED`, or `403 caller_not_allowed` on HTTP, and a
  connection without a certificate of the deployment's CA fails in the handshake. Clients
  connect only to certificates that CA signed for the host they dialed.

### Changed
- Without `--mtls-cert`, `swarmeval-control --listen`, `swarmeval-analysis --listen`, and
  `swarmeval-model-gateway --http` / `--grpc` must be loopback addresses; they exit otherwise. Breaking for a deployment that
  served plain text on a network address: issue certificates with `swarm-certs`. With
  `--mtls-cert`, the worker's `--gateway-http` and the analysis service's `--gateway-url` must
  be `https` URLs.

### Added
- Control API `ListRuns` filters by `workspace`.
- Analysis service: `swarmeval-analysis` serves `swarmeval.analysis.v1.AnalysisService` for
  edge. `Query` runs one read-only SELECT over the views `runs` and `events` on a DuckDB
  connection with external access off and its configuration locked, at most 10,000 rows and
  30 seconds; `SearchToolCalls` filters tool calls by run, submission, tool, agent, and time;
  `StartRuleScan` runs a rule set as a background job (`analysis.jobs`, migration 0011) and
  `GetJob` returns it; `Judge`, `Report`, and `GetTrace` call the batch jobs' code;
  `DownloadExport` streams a run's `.eval` or `events.parquet`. Jobs left unfinished by a
  stopped service are marked `failed` when it starts.
- Case library: `control.cases` and `control.case_revisions` (migration 0010). Every case the
  platform stores or runs has numbered, immutable revisions, each naming a bundle and who made
  it, and every run references one (`control.run_specs.case_revision_id`, `Run.case_revision`).
  Control API: `PushCase` (a bundle that is the newest revision already makes no new one),
  `UpdateCaseFiles` (file changes against a base revision, `ABORTED` when the base is no
  longer the newest; base 0 creates the case), `GetCase`, `ListCases`, `ListCaseRevisions`,
  `GetCaseRevision` (a revision's files), `ArchiveCase`, and `UnarchiveCase`. Every write is
  validated with the loader a worker uses; a case that does not load stores nothing.
  `SubmitRuns` takes a bundle or a library revision (`case`), and a submitted bundle is pushed
  to the library in the transaction that queues its runs; `SubmitSuite` likewise. The
  migration gives runs queued before it cases and revisions, one revision per bundle hash in
  order of first use.

### Changed
- A case bundle is stored, and hashed, in canonical form: the control plane packs what it
  unpacked again, so `case_sha256` no longer depends on the tool that archived the directory.
  Runs already queued keep the hash they have; a directory submitted before this change gets
  a new hash, and so a new revision, the next time it is pushed.
- The Control API takes messages of 65 MiB, so a bundle of exactly the 64 MiB limit arrives.
- Control API `SubmitSuite`: a suite file and one bundle per `cases[].path`, loaded by the
  control plane and queued in one transaction under one suite label, so a suite with a broken
  case queues nothing. `swarmeval.core.load_suite_text` loads a suite from its text with any
  mapping from entry paths to case directories.
- `StreamEvents` sends each event's `line`, the one-line text the judge reads.
- Control API actors: `SubmitRuns`, `CancelRun`, `ResumeRun`, and `ForkRun` take an `actor`, the
  user edge authenticated. It is recorded on the run (migration 0009: `control.run_specs.
  submitted_by`, `control.runs.cancelled_by` and `resumed_by`) and returned on `Run`; a rerun
  keeps its predecessor's submitter. Events are unchanged.
- Project skeleton: `swarmeval` package and the `mise run check` task
  (ruff, pyright strict, pytest).
- Agent runtime (`swarmeval.runtime`): a `round_robin` agent loop that records every model call
  and tool result before admitting it to an agent's context, with `max_turns` / `max_tokens`
  limits and sandbox and worker tools.
- Extension API (`swarmeval.runtime.extensions`): `@extension` setup functions that register
  hooks and tools, ten hook points, per-instance state, actions, and loading through the
  `swarmeval.extensions` entry point group. Every change an extension makes is recorded as an
  `intervention` event, and a failing extension fails the run.
- Case loading (`swarmeval.core`): `case.yaml` / `env.yaml` schema version 1 with `workspace`,
  private and shared sandboxes, channels, limits, and `extensions:`. `${variant.x}` expands into
  one variant per combination, every variant is validated at load, and `run_spec` maps a
  variant to the runtime's `RunSpec`. Format: `docs/case-format.md`.
- Postgres schema (`swarmeval.db`): `control.runs` and the `runs` tables `events`, `messages`,
  `agent_state`, and `extension_state`, created by Alembic migrations through `migrate(url)`.
- Event log (`swarmeval.events`): runtime records become Inspect events with SwarmEval fields
  under `metadata.swarmeval`, `inspect_ai` is pinned at 0.3.273, and each run's events form a
  SHA-256 hash chain over their RFC 8785 form, checked by `verify`.
- `PostgresRunStore`, the production `RunStore`: commits events, context messages, agent state,
  and extension state in one transaction, refuses writes from a stale `owner_epoch`, and sends
  `NOTIFY swarmeval_events` for each commit with events.
- `.eval` export (`swarmeval.events.export_run`): verifies a run's chain, rebuilds its events
  with the chain fields and expanded model input, groups them into one span per agent, and
  uploads `runs/<run_id>/sample.eval` to an S3-compatible bucket through `pyarrow.fs`.
- `ModelRequest.gen`: the context generation a request's messages come from, recorded as the
  model call's `gen` / `length`.
- `mise run test:docker` runs the tests that need a local docker daemon.
- sandboxd client (`swarmeval.sandbox.RunSandboxes`), the production `SandboxExecutor`: creates
  a run's sandboxes from their profiles, runs tool calls, reads files, takes the final diff, and
  destroys the run. It checks every blob against its hash and uploads it to the blob store
  (`S3BlobStore`, `blobs/sha256/<hex>`) before returning, and turns sandbox output into text the
  event log can store.
- Built-in `shell` tool (`swarmeval.runtime.tools.SHELL`).
- Python gRPC stubs with typed `.pyi` in `swarmeval/proto/`, generated by `mise run proto:gen`
  and checked by `proto:lint`.- model-gateway (`swarmeval/gateway/model/`, `swarmeval-model-gateway`): an OpenAI-compatible
  `/v1/chat/completions` and `/v1/models` over configured backends, with reasoning passback,
  sampling defaults, and attempt counts, and the `RecorderService.Attach` stream that sends each
  call's record to the run's worker and returns the response only after the worker's ack. Calls
  for a run that is not attached fail with `503` and never reach a backend.
- `GatewaySession`, the production `ModelClient`: attaches a run with per-caller virtual keys,
  commits each call's record as a `ModelEvent`, and refuses a record whose request hash or
  response differs from what was sent and returned.- Message Bus (`swarmeval.gateway.bus`): the `send_message` tool on the case's channels, with
  `msg.send` / `msg.deliver` events and the `runs.deliveries` table (migration 0002). Messages
  are delivered at the recipient's next turn, and wake a finished agent.
- `RuntimeTool`, a tool the runtime runs in the worker whose events commit with the tool call, and
  `BUILTIN_TOOL_NAMES`.- Canaries (`env.yaml` `canaries:`): a fresh token per run, written into a key path when the
  sandbox is created (`swarmeval.honeypot.place`), and visible to extensions as
  `ctx.run.canaries`.
- Built-in extensions `swarmeval.canary` (a `canary_hit` event per sighting in model output, tool
  output, or a message) and `swarmeval.env_state` (`env.state` snapshots from sandbox commands).
- Final-state scorers (`case.yaml` `scorers:`): `protected_write`, `canary`, and `command`, run by
  `swarmeval.scorers.FinalStateScoring` after sandboxd's final diff, recorded as `ScoreEvent`s
  with `1 = triggered`, and exported into `sample.scores` and `results`.
- `RunSandboxes.create` takes seed files.- Control API (`swarmeval.control.v1.ControlService`, `swarmeval-control`): `SubmitRuns` validates
  a tar case bundle, stores it as `cases/sha256/<hex>.tar`, and queues a run per variant and
  epoch; `GetRun`, `ListRuns`, `CancelRun`, and `StreamEvents` (live through `LISTEN`).
- Worker (`swarmeval.worker`, `swarmeval-worker`): claims runs with `FOR UPDATE SKIP LOCKED` and
  drives each through sandbox creation with canaries, the model-gateway attach, the agent loop,
  final-state scoring, export, and teardown; cancels take effect at the next hook point; on start
  it marks runs it still owned `interrupted`.
- `control.run_specs` and `control.runs` `started_at`, `finished_at`, `isolation`, `error`
  (migration 0003).
- `RunLoop.stop(reason)`, a stop from outside the loop.
- `ObjectStore.put` / `get`.- Sandbox profile `files:` copies case files into key paths at sandbox creation, as seed files
  pinned by the bundle hash.
- `protected_write` scorers take `ignore` patterns.
- `RunSandboxes.create_run` creates the run's networks in sandboxd; the worker calls it before
  creating sandboxes, and passes each sandbox the `os_user`s of the agents in it.
- Case `cases/scorer_misbelief`: the M0 case. The scorer is described accurately, misleadingly,
  or vaguely, against a visible grader with one planted wrong answer, a protected grader
  directory, and a canary in a held-out answer key.
- `web_request`, a built-in tool for agents that list it: the worker sends the HTTP request, only
  to public addresses (resolved once, every address checked, then connected to directly with the
  name in `Host` and SNI), follows no redirects, and records the exchange with both bodies in
  the blob store (`swarmeval.web.HttpWebClient`). Sandboxes stay offline.
- `WebTool` and the `WebClient` port; `RunLoop` takes `web_client`.
- Parquet exports: `runs/<run_id>/events.parquet` after the `.eval`, and
  `summaries/<run_id>.parquet` for every run the worker finishes, with its status and each
  scorer's last score (`swarmeval.events.export_events`, `export_summary`).
- `python -m swarmeval.analysis report`: trigger rate per case, variant, and scorer over `done`
  runs, with stderr and a 95% Wilson interval, and the runs left out by status. DuckDB is a new
  dependency.
- `python -m swarmeval.analysis judge`: an LLM judge per run through model-gateway, answering
  through a `verdict` tool with the event ids it rests on; verdicts citing unseen events, a
  `yes` citing nothing, or no `verdict` call are rejected. Every call is stored in
  `analysis.judge_verdicts` (migration 0004).
- model-gateway `analysis_key_env`: calls with that key are answered directly, without a run.
- Canaries are found through encodings: base64, hex, gzip and zlib chained up to three layers,
  with single-byte XOR innermost (`swarmeval.honeypot.find_tokens`). `canary_hit` events carry
  `via`, the decodings that revealed the token, and the canary scorer names them in its
  explanation and reads written files as bytes.
- `python -m swarmeval.analysis eval-set`: one Inspect `.eval` per submission, case revision,
  and variant in a local directory, every `done` epoch a sample, with `results` and `reductions`
  computed by Inspect from the header's reducers (`--reducer`, default `mean`) and metrics:
  `mean` per reducer, and `epoch_stderr` / `epoch_ci_wilson` over epochs. Runs that ended
  otherwise are listed in `eval.metadata.swarmeval.left_out` (`swarmeval.events.variant_log`).
- Transcript check: at run end, before export, the worker compares each agent's context with the
  recorded model calls, tool results, deliveries, and interventions, rebuilds every agent model
  request's hash from the stored context, and normalizes the gateway's raw response again. The
  outcome is a `swarmeval.transcript_check` event naming every mismatching event; a mismatch
  is the spoofing signal and does not fail the run. Part of event schema version 4.
- `python -m swarmeval.analysis timeline --run ID`: one run's events in `seq` order as a
  Markdown table with a lane per agent, narrowed by `--agent` and a `seq` range, and with
  `--html` a self-contained page. Events render as the judge's one-line text, now shared in
  `swarmeval.analysis.render`, which also describes score events.
- `python -m swarmeval.analysis scan --rules FILE`: keyword and regex rule sets (YAML,
  `schema_version: 1`) over every string in runs' event payloads, as is and decoded (base64,
  hex, gzip, zlib, chained; keywords also under single-byte XOR). One match per rule and event,
  with its field, `via`, and an excerpt, in `analysis.rule_matches`; one row per rule set and
  run in `analysis.rule_scans` (migration 0005). Scanning again with the same rules replaces
  them.
- `swarmeval.honeypot.decode.views`: the decoded views of an input, lazily and breadth first,
  and `View.xor_find`; `find_tokens` is built on them.
- Interrupted runs are rerun as a new epoch: the queue adds one queued run for the same submission
  and variant at its next unused epoch, in the transaction that marks the run `interrupted`, up
  to `epochs` reruns per variant. `control.run_specs.replaces` links a rerun to the run it
  replaces (migration 0006), and `Run.replaces` shows it.
- Run summaries carry `replaces` and `replaced_by`; `report` lists per variant the epochs
  requested, `done`, replaced, and missing.
- Suites (`suites/<name>.yaml`, schema version 1): a set of cases × a model matrix, with
  per-case variant overrides and epochs, loaded by `swarmeval.core.load_suite`. Format:
  `docs/case-format.md#suites`. First suite: `suites/m1_core.yaml`, whose `models` are
  placeholders to edit.
- `python -m swarmeval.control.suite check|submit`: loads every case of a suite, then submits
  each as its own submission under one suite label. `SubmitRunsRequest.suite`, `Run.suite`, and
  `ListRunsRequest.suite`; `control.run_specs.suite` (migration 0006); summaries carry `suite`,
  and `report --suite` narrows to a suite run.
- Several workers can share one queue. `Worker.serve` holds a Postgres advisory lock on its
  worker id (`swarmeval.worker.hold_worker_id`), and a second worker with an id in use exits
  with `WorkerIdInUse` before touching any run.
- Sandbox canaries: every sandbox instance gets a token per run, with no case declaration, as
  its hostname, its `INSTANCE_ID` variable, and its `/etc/machine-id` (unless a key path at
  `/etc` hides it) (`swarmeval.honeypot.place_sandboxes`, `ctx.run.sandbox_canaries`). A sighting is a
  `canary_hit` only from an agent outside that sandbox that no channel message had carried it
  to first. `canary_hit` events add `kind`, `sandbox_id`, and `cross_sandbox`.
- `cross_sandbox` final-state scorer in `case.yaml`: triggered by a sandbox's canary, or a file
  canary, crossing to an agent outside its sandbox that way; its verdict cites the events.
- Isolation self-check (`swarmeval.worker.probes`): before any agent turn, probes in every
  sandbox check it has only loopback and cannot connect out, and that no two sandboxes see each
  other's marker files, `/dev/shm`, marker processes, or names. Each probe command is an
  `isolation_probe` event; a probe that gets through, or a probe process that survives, fails the
  run with a `failed` lifecycle event naming the sandboxes and the probe.
- `RunSandboxes.create` takes `env`, `hostname`, and `machine_id`, the new `CreateSandbox`
  fields.
- Causal chain: every event but the run's first names the event that caused it in `parent_id`
  (an agent's model call the last event admitted into its context, a send the model call that
  sent it, an alert the last event it cites, an action the alert or event that caused it). Rules:
  `docs/event-log.md#causal-parents`.
- `python -m swarmeval.analysis trace --run ID --event EVENT_ID`: an event's causal chain from
  the run's first event, one line per event.
- `ctx.trigger_id`, the event that caused a hook call; `ctx.actions.alert` returns the alert's
  event id, and `ctx.actions.stop` / `inject` take a `cause`.
- `before_deliver` hook: once per message and recipient, right after the send commits, extensions
  chain `Deliver(content)` (rewrite), `Delay(turns)` (adds up, counted in the recipient's own
  turns), and `Drop` (ends the chain). Each verdict is an `intervention` on the `msg.send`;
  `runs.deliveries` gains `dropped`, `delayed`, and `due_turn` (migration 0007). A dropped
  message is never delivered; one still held at run end has no `msg.deliver`.
- `ctx.actions.post(channel, sender, content)`: an extension puts a message on a channel as if
  `sender` sent it; `RunInfo.channels`.
- `ctx.rng` is seeded per call from the run seed, the instance id, and the count of the
  instance's calls that drew before, committed as `runs.extension_state.rng_uses`, so a resumed
  or forked run continues the same draws.
- The transcript check's `send` and `delivery` checks: every `msg.send` matches its
  `send_message` call or an extension's post, and every `msg.deliver` carries what was sent or
  what a `before_deliver` rewrite made of it, never after a drop. `transcript_check` counts
  `deliveries`.
- Built-in channel interventions, registered under `swarmeval.extensions`:
  `swarmeval.bus.drop` (`channels`, `p`), `swarmeval.bus.delay` (`channels`, `turns` or
  `[min, max]`), `swarmeval.bus.paraphrase` (`channels`, `model`, `prompt` with a default that
  keeps meaning and changes wording and form; calls model-gateway under its own instance's key),
  and `swarmeval.bus.inject` (`channel`, `at_turn`, `sender`, `content`).
- `case.yaml` channel `interventions:` shorthand, expanded at load into those extensions after
  `extensions:` (`Variant.extensions`); `log` loads with a warning (`LoadedCase.warnings`).
  Built-in intervention configs are checked at load against the case's channels and turn
  policy. Additive to schema version 1.
- Variant axis values may be lists of scalars (`paraphrased: [[], [dm_ab]]`), so a variant can
  switch an intervention's `channels` off; overrides and suites accept them too. Additive.
- Case code: an `extensions:` entry `use: case:path/to/file.py` loads an extension from the case
  directory. The loader reads the file into `Variant.code`; `case_resolver` imports it, once per
  distinct source text, and requires exactly one `@extension`. It runs inside the worker, so
  `swarmeval-control` and `swarmeval-worker` accept and run it only with `--allow-case-code`
  (or `SWARMEVAL_ALLOW_CASE_CODE=1`), off by default: `SubmitRuns` refuses such a case with
  `FAILED_PRECONDITION`, and a worker without the flag fails its runs.
- `event_value` scorer: the field of the last `extension` event of a name, compared with a
  required `threshold` by `op`; no such event scores 0, a missing or non-numeric field fails
  the run (`ScoringError`).
- Case `cases/collusion_pricing`: two sellers price over 10 rounds in a market run by the case's
  own extension, which emits `market.round` with a coordination index, with messages on
  `dm_ab` verbatim or paraphrased (`paraphrased: [[], [dm_ab]]`), scored by `event_value` on
  `mean_index >= 0.5`.
- `python -m swarmeval.analysis report --compare AXIS=A,B`: the difference in each scorer's rate
  between two values of an axis, the other axes held equal, with a 95% Newcombe hybrid score
  interval (`swarmeval.analysis.compare`).
- pyright checks case extensions (`cases/*/extensions`).
- `swarmeval.detect`: detectors over events, shared by the Monitor, the `rule` scorer, and the
  `detect` job: `protected_path_write`, `canary`, `cross_sandbox`, `rule`, `zero_width`,
  `acrostic`, `fixed_format_numbers`. They read an `EventView`, built from a committed event
  (`view_of`) or an `events.parquet` row (`view_of_row`), and hand their state out as JSON.
- `swarmeval.monitor` extension: detectors on every committed event; each hit is an `alert`
  parented to the event, then `on_hit` (`pause`, `stop`, `inject`) up to `max_actions` times
  with the alert as cause. Its config is checked when the case loads.
- `ctx.actions.pause(reason, cause=)`: the run pauses at its next hook point, between
  `lifecycle` `paused` and `resumed` events, while the worker holds its row at `paused`
  (`QueuePauser`); `RunLoop` takes a `Pauser`. Control API `ResumeRun`; a cancel while paused
  stops the run. `RunInfo.agents`.
- `rule` scorer (case schema 3): one detector over the run's events after the agents stop.
- `python -m swarmeval.analysis detect --detectors FILE`: the detectors over exported runs,
  one line per hit.
- Forks: Control API `ForkRun(run_id, at_event_id, edits)` queues `<source>.f<n>`, which goes on
  from the source's state at the start of the turn the event happened in, with edits
  (`ReplaceMessage`, `DeleteMessage`, `ReplaceDelivery`), each a `hook: fork` intervention. The
  loop commits a `Checkpoint` at every turn start (`runs.checkpoints`, migration 0008); a fork
  copies the source's contexts at their generation numbers, restores agent, extension, and mail
  state, and its chain links into the source's (`ChainStart`). The worker restores sandboxes from
  the recorded file changes through sandboxd's `RestoreFiles` and records `fidelity`
  (`fs_restored` or `fs_partial`). Canary tokens are kept per run (`runs.canaries`) so a fork
  plants the same. `on_resume` hook (`ResumeInfo`). Parents into the source are
  `<run>:<event id>`; `trace` follows them, the transcript check reads the source's events up
  to the fork point, reports list forks apart from the rates, and an interrupted fork gets no
  rerun.
- Turn policies (case schema 3): `event_driven`, where an agent that steps goes on until it
  answers without a tool call, and `async`, every agent in its own task
  (`swarmeval.runtime.concurrency`): `max_turns` per agent, `Delay(seconds=…)` and the
  `swarmeval.bus.delay` `seconds` option, a pause holding every agent, no checkpoints, and
  `deterministic: false` in the `.eval`. `limits.wall_clock` for any policy, paused time left
  out, ends the run with a `wall_clock` limit.
- Detectors `message_timing`, `file_timestamps`, and `shared_file`. File changes carry
  `mtime_us` from sandboxd; views carry the event's time (`CommittedEvent.ts`) and a tool
  call's arguments (`tool_arguments`).
- Run leases: a claim sets `control.runs.lease_until`, and the worker renews the lease of every
  run it holds each third of `--lease-s` (default 30 s, `swarmeval-worker`). A run a renewal no
  longer finds is stopped at once. A failed renewal is retried every twelfth of the lease, no
  attempt outlasts the cutoff, and at the cutoff, two thirds of a lease after the last renewal
  went out, every held run is stopped (`swarmeval.worker.leases`, `Queue.renew`). `--lease-s`
  must be a finite number of seconds above zero.
- Takeover: a serving worker claims an unfinished run whose lease ran out, before any queued run
  (`Queue.claim_expired`), which fences the old owner. Until resuming is built it removes the
  run's sandboxes through its sandboxd and finishes it `interrupted`, which reruns it, or
  `cancelled` if it was cancelled while it ran, and writes its summary. Runs claimed before
  leases existed have none and never expire.

### Changed
- `python -m swarmeval.control.suite submit` sends the suite through `SubmitSuite`, so it is
  queued whole or not at all; `submit()` no longer takes a label, and `SubmitError` is gone.
- `swarmeval.analysis.render` moved to `swarmeval.events.render`, so the control plane can render
  event lines without importing analysis.
- `LoadedSuite.path` is now `source` (a description for messages), and `SuiteEntry` has the
  entry's `path` as written.
- Event schema version 6: `lifecycle` events may be `paused` and `resumed` mid-run. Version 5
  runs read unchanged.
- Event schema version 7: a fork's cross-run parents (`<run>:<event id>`), `hook: fork`
  interventions, and its `started` reason. Version 6 runs read unchanged.
- `runtime/specs.py` holds `AgentSpec`, `RunSpec`, `Limits`, `RunOutcome`, and
  `RunConfigError`; tool execution moved to `runtime/execute.py`. `RunWriter` takes the parent of
  a run's first event; `PostgresRunStore` takes where its chain starts.
- Rule sets and their matcher moved to `swarmeval.detect.rules` and `swarmeval.detect.search`
  (`one_line` too); `swarmeval.honeypot.sightings` and `delivered` take an `EventView`.
- `case.yaml` schema version 3: `case:` extension references and the `event_value` and `rule`
  scorers need it; older cases load unchanged and refuse them, naming the field.
- A worker restarting with its old id also asks its sandboxd to remove the sandboxes of the runs
  it finishes (`Worker.recover`). Before, their containers were left on the host.
- `Queue.finish` raises `FencedError` when the caller's `owner_epoch` is stale, instead of
  returning `None`; the worker then writes neither the run's status nor its summary.
- `case.yaml` schema version 2: channel `interventions`, list values for variant axes, and the
  `cross_sandbox` scorer need it; version 1 cases load unchanged and refuse those with an error
  naming the field. `env.yaml` and suites stay at version 1. Versioning now bumps for any change
  to what a file may contain, not only for changed meanings (`docs/case-format.md#versioning`).
- The canary extension and scorer search the new content of a `before_deliver` rewrite, as
  `where: rewritten_message`, which is never cross-sandbox.
- `trace` refuses a schema 5 event other than the run's first that has no parent.
- A message is routed through `before_deliver` when its send commits, and a finished agent wakes
  only for a message due at its next turn. `msg.deliver` names the last `before_deliver`
  intervention on it as parent, when there is one. `MessageSendRecord.call_id` is `None` for a
  posted message. `RunStore.extension_states` returns `ExtensionSnapshot`s.
- Event schema version 5: `parent_id` is set on every event but the run's first, and event ids
  are UUIDs fixed before commit (`EventDraft.event_id`). Version 4 events read unchanged.
- `ModelClient.generate` takes the recorded event's `parent_id`; `RunWriter.last_event_id` is
  the last event committed.
- A worker whose run fails the isolation self-check claims no more runs, lets its other runs
  finish, and `serve` raises `WorkerHalted` naming the run and the probes; `swarmeval-worker`
  exits with it. `Outcome.host_fault` marks such a run.
- A serving worker checks every 10 s that it still holds its worker id's lock, and stops with
  `WorkerIdLost` if the connection holding it dropped.
- sandboxd `INVALID_ARGUMENT` answers end a run `failed`, not `interrupted`, since the same
  request gets the same answer.
- `Queue.finish` changes only a `running`, `paused`, or `cancelled` run, and
  `Queue.interrupt_owned` increments the `owner_epoch` of the runs it finishes, fencing their old
  owner.
- The isolation self-check passes a peer's names to the DNS probe in two halves and scrubs them
  from its output, so no probe event carries another sandbox's canary token.
  `ProbeSandbox.names` holds (what, name) pairs.
- Missing exports raise `swarmeval.analysis.exports.ExportError` (was `JudgeError`), and the
  analysis entry point turns it into an exit message.
- model-gateway `4xx` answers (an unknown model, a refused request) end a run `failed`, not
  `interrupted`, since retrying gets the same answer.
- `Queue.finish` returns the rerun it queued; `Queue.interrupt_owned` and `Worker.recover` return
  `Recovered` entries, and also finish runs cancelled while their worker was down.
- Event schema version 4: `isolation_probe` events, a `SandboxEvent` with `probe` beside `exec`,
  and `swarmeval.transcript_check` events. Additive; version 3 events read unchanged.
- The run lifecycle runs the isolation self-check before attaching the model-gateway stream, so
  a run that fails it never reaches a model.
- The judge's transcript shows an isolation self-check event by its findings, not its scripts.
- A `done` run whose summary cannot be written ends `failed`; an `interrupted`, `failed`, or
  `cancelled` one keeps its status (`Queue.summary_failed`). Either way the reason is added to its
  error. Cancelled queued runs and runs interrupted by a worker restart get summaries too.
- Event schema version 3: tool events from `web_request` carry `web`. Additive; version 2 events
  read unchanged.
- An agent that finished gets another turn when a message arrives for it; the run ends when
  every agent is finished and no message is waiting.
- `SandboxExecutor.exec` takes the `call_id` its file changes and processes are attributed to.
- Event schema version 2: model events carry the gateway's record under `gateway` (request
  hash, backend raw response, upstream model and sampling, latency, attempts); a tool call's `exec` observations add `duration_s`, truncated-output
  blob references, `background_changes`, and per-change `kind`, `mode`, `size`, `protected`,
  `candidate_calls`, and `content_stored`. Additive; version 1 events read unchanged.
