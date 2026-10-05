# orchestrator

Python. It loads cases, queues and schedules runs, and hosts every run's agent loop. It is the only
writer of a run's events and state. Its place among the services is in
[architecture.md](../architecture.md). The stored event format is in
[event-log.md](../event-log.md).

**Status:** the M0 part is built: case loading, the Control API, the queue, a worker that drives
each run from claim to export, the agent loop ([agent-runtime.md](../agent-runtime.md)), the
Message Bus, canaries, and final-state scorers. Of M1, `web_request`,
several workers, reruns of interrupted runs, suites, per-sandbox canaries, and the isolation
self-check are built. Of M2, the causal chain, channel interventions, case code, and the
`event_value` scorer are built; fork, the online Monitor, and the async and event-driven turn
policies are built too: the online Monitor, the `rule` scorer, pauses, `ResumeRun`, forks
(`ForkRun`), and the `event_driven` and `async` turn policies. Of M3, leases and taking over a run
whose lease ran out are built; resuming a taken-over run, fidelity, and pausing on infrastructure
failure are not yet, so a taken-over run is rerun. Items marked
*(proposed)* go beyond what the specs decided; they are listed under [Not settled](#not-settled).

## Roles

One image, two entry points (`swarmeval-control`, `swarmeval-worker`). They scale separately.

```bash
export SWARMEVAL_DATABASE_URL=postgresql://swarmeval:…@db:5432/swarmeval
export SWARMEVAL_S3_ACCESS_KEY=… SWARMEVAL_S3_SECRET_KEY=…
swarmeval-control --s3-endpoint rustfs:9000 --s3-bucket swarmeval --listen 0.0.0.0:7090
swarmeval-worker --s3-endpoint rustfs:9000 --s3-bucket swarmeval \
  --sandboxd sandboxd:7071 --gateway-http http://model-gateway:7080 --gateway-grpc model-gateway:7081
```

The control plane migrates the database when it starts. One bucket holds case bundles, blobs,
and exports. Credentials come only from the environment.

| Role | Replicas | State | Does |
|---|---|---|---|
| Control plane | Any number; stateless | `control` schema | Control API, case validation, variant expansion, queue, live event streams |
| Worker | Any number; each hosts many runs | `runs` schema, per run it owns | Claims runs, drives agent loops, sequences and commits events, exports |

The split is by function, never by run. One run's agent loops, sequencing, and writes all live in
the worker that owns it. Runs have at most a dozen or so agents, and the bottleneck is model
inference, so one asyncio process per worker is enough.

## Code layout

| Package | Holds |
|---|---|
| `swarmeval/core/` | Case and env models, loader, variant expansion |
| `swarmeval/control/` | Control API servicer (`service.py`, and `case_rpcs.py` for the case RPCs), case bundles, the case library (`cases.py`), the queue (shared with the worker), live event wakeups |
| `swarmeval/worker/` | Worker main loop, run lifecycle, and the isolation self-check |
| `swarmeval/runtime/` | Turn policies, the ReAct agent loop, tool dispatch, extensions. Knows no database or service |
| `swarmeval/gateway/bus/` | Message Bus: channel ACLs, deliveries, interventions |
| `swarmeval/detect/` | Detectors over events, their configs, `EventView` with its online and offline adapters, and rule sets over decoded text; shared with the `rule` scorer and analysis |
| `swarmeval/monitor/` | The `swarmeval.monitor` extension: detectors online and the actions a hit takes |
| `swarmeval/honeypot/` | Canary generation and matching (through encodings), the `swarmeval.canary` and `swarmeval.env_state` extensions, honeypot templates (with the network capability, later) |
| `swarmeval/db/` | Table definitions for `control` and `runs`, engines, Alembic migrations (`migrate(url)`) |
| `swarmeval/events/` | Records to Inspect events, the hash chain, the Postgres `RunStore`, export, and an event as one line of text (`render`, which the judge, the timeline, and `StreamEvents` share). The only `inspect_ai` import |
| `swarmeval/scorers/` | Final-state scorers, the `rule` scorer among them |

Generated gRPC stubs live in `swarmeval/proto/`, generated from `proto/` by `mise run proto:gen`.
The sandboxd client is `swarmeval/sandbox/`.

## Control plane

### Control API

gRPC service `swarmeval.control.v1.ControlService` (`proto/swarmeval/control/v1/control.proto`)
on the internal network *(RPC names proposed)*. With `--mtls-cert`, `--mtls-key`, and `--mtls-ca`
it accepts only `edge` and `operator` certificates
([service identity](../architecture.md#service-identity)); without them `--listen` must be a
loopback address. The worker takes the same three flags to call sandboxd and model-gateway,
and then `--gateway-http` must be an `https` URL. A case bundle may be up to 64 MiB, and a
message 1 MiB more, so that a bundle of exactly the limit still fits with the rest of its
request.

| RPC | Does | From |
|---|---|---|
| `SubmitRuns` | Takes a case, as a bundle or as a [library](#case-library) revision (`case`: workspace, case id, revision, 0 = newest), exactly one of the two; variant overrides (axis → list of values), epochs (0 = the case's), and an optional [suite](#suites) label. A bundle is validated and pushed to the library as `PushCase` pushes it, in the transaction that queues the runs. Enqueues one run per variant and epoch; returns the submission id, the run ids, and the revision they use. A case that does not load is `INVALID_ARGUMENT` with the loader's message; an unknown case or revision is `NOT_FOUND`; an archived case is `FAILED_PRECONDITION` | Built |
| `GetRun`, `ListRuns` | Status, variant and its values, epoch, the run it [reruns](#reruns), owner, isolation level, error, timestamps. `ListRuns` filters by submission, suite label, case, workspace, and status, newest first; the filters apply before the limit | Built. Fidelity arrives with recovery (M3) |
| `CancelRun` | Marks cancelled. A queued run never starts; a running one stops at its owner's next hook point, is not scored, and is still exported. A finished run is `FAILED_PRECONDITION` | Built |
| `ResumeRun` | A run paused for a person (a Monitor's `pause`) goes on from the hook point where it stopped. A run that is not paused is `FAILED_PRECONDITION` | Built |
| `ForkRun` | A new run that goes on from a finished run's state at the start of the turn an event happened in, with edits: replace a message's text, delete a user message, or replace an undelivered message's content ([forks](#forks)). Returns the fork, `<source>.f<n>`, queued. Only a run that ended `done` or `cancelled` (an exported one) can be forked. An unknown run or event is `NOT_FOUND`; any other source run, an event before the first turn, a turn that began with extension background work (`ctx.spawn`) still running, a run recorded before forks were possible, or an edit that does not fit is `FAILED_PRECONDITION`; a malformed edit is `INVALID_ARGUMENT` | Built |
| `SubmitSuite` | Takes a [suite](#suites) file and one bundle per `cases[].path` it names, loads the suite, and queues every case's runs in one transaction under one suite label; returns the label and one submission per entry. A suite that does not load, a path with no bundle, or a bundle the suite does not name is `INVALID_ARGUMENT`, and nothing is queued | Built |
| `StreamEvents` | Server stream of a run's events after a given `seq`, live while it runs; ends once the run has finished and every event was sent. Each event comes with its stored payload and its `line`, the one-line text the judge reads (`swarmeval.events.render`), cut at 2,000 characters | Built |
| `PushCase`, `UpdateCaseFiles`, `GetCase`, `ListCases`, `ListCaseRevisions`, `GetCaseRevision`, `ArchiveCase`, `UnarchiveCase` | The [case library](#case-library) | Built |

`SubmitRuns`, `CancelRun`, `ResumeRun`, and `ForkRun` take an `actor`: the user [edge](edge.md)
authenticated, empty for internal tooling. It is recorded on the run, not in its events, and `Run`
returns it as `submitted_by` (a rerun keeps its predecessor's), `cancelled_by`, and `resumed_by`
(the last resume) ([M4 spec](../../spec/2026-10-03-m4-console/README.md) decision 2). The case
writes take one too: it is recorded on the revision they make. Who archived or unarchived a case
is only logged.

Run ids are `<case>.<submission>.v<variant>.e<epoch>`. Override numbers travel as protobuf doubles;
a whole number becomes an int again.

### Case bundles

`SubmitRuns` carries the case directory as a tar archive. The control plane validates it with
the same loader the worker uses, packs what it unpacked again in one canonical form
(`swarmeval.control.bundles.pack`: files and links sorted, times and owners zeroed), and stores
that in object storage as `cases/sha256/<hex>.tar`. The hash is of the canonical bytes, not of
what was uploaded, so the same files, modes, and links are the same bundle whichever tool
archived them: Python's `tarfile` and the CLI's Go packer write different bytes for one
directory.
Each run row references that hash, and the [library](#case-library) revision that names it. Control plane and workers share no disk, and the hash pins the
exact prompts, hooks, and data a run used *(proposed)*. Bundles are extracted with tarfile's
`data` filter: `..`, links that leave the directory, and device files are refused, and absolute
member paths land inside the directory. `case.yaml` must be at the archive's root.

### Case library

Built. Every case the platform stores or runs is in the library, with its history
([M4 spec](../../spec/2026-10-03-m4-console/README.md) decision 6).

- **Cases.** `control.cases` has one row per `workspace` and `case_id`, the `workspace` and `id`
  of the `case.yaml`. The same id in two workspaces is two cases.
- **Revisions.** `control.case_revisions` numbers a case's revisions from 1. Each names a
  [bundle](#case-bundles) by hash, the `actor` of the request that made it, and an optional
  note of at most 1,000 characters. A row is never updated or deleted, and neither is its
  bundle: every run references one revision (`control.run_specs.case_revision_id`), so they are
  part of the evidence. `Run` returns the number as `case_revision`. A rerun and a fork use
  their source's revision.
- **Push** (`PushCase`, and a bundle given to `SubmitRuns` or `SubmitSuite`). The bundle is
  loaded as a worker will load it. If its canonical bytes are the case's newest revision, that
  revision is used; otherwise it becomes the next one. An archived case is refused before
  anything is stored. The first push creates the case. A case that
  does not load is `INVALID_ARGUMENT` and nothing is stored; [case code](#case-code) on a
  deployment that runs none is `FAILED_PRECONDITION`, as before. A revision pushed by a
  submission is written in the transaction that queues its runs.
- **Edit** (`UpdateCaseFiles`, for the console). Takes the revision the changes were made
  against and a list of changes, each a path with new content or a delete. The control plane
  unpacks the base bundle, applies the changes, packs it again, and validates the result as a
  push is validated, so a revision that does not load is never stored. A base that is not the
  newest revision is `ABORTED`, with the newest revision's number in the message; it is checked
  under a row lock on the case, so of two edits at once exactly one wins. Base 0 creates a case
  that does not exist from the written files alone. Changes that leave every file as it was
  make no revision. Limits and refusals (`INVALID_ARGUMENT`): a written file is at most 1 MiB,
  and the packed case at most 64 MiB; paths are relative with `/` separators, without `.`, `..`,
  or `__pycache__` parts, and may not pass through a file or a link; a path may appear once;
  deleting a file that is not there is refused; `case.yaml` must still name the same
  `workspace` and `id`. A written file keeps its mode, a new one gets 0644, and a link written
  to becomes a regular file.
- **Read.** `ListCases` (by workspace and id; archived cases only with `include_archived`) and
  `GetCase` return each case with its newest revision. `ListCaseRevisions` is newest first.
  `GetCaseRevision` returns one revision (0 = newest) with its files: path, content, mode, and
  for a symbolic link its target. Diffs are the client's to compute.
- **Archive.** `ArchiveCase` sets `archived_at`: the case leaves the default list and takes no
  pushes, edits, or runs (`FAILED_PRECONDITION`). Nothing is deleted, old revisions stay
  readable, and `UnarchiveCase` takes it back.
- **Runs from before the library.** Migration 0010 gave them cases and revisions: one revision
  per bundle hash a case's runs had used, numbered in the order the hashes were first used,
  with the first run's `submitted_by` as actor.

### Suites

A [suite](../case-format.md#suites) runs as one submission per case, all carrying one suite
label. Users submit it with `swarm run suites/m1_core.yaml` through [edge](edge.md#swarm-cli);
operators on the internal network can use `python -m swarmeval.control.suite`:

```bash
uv run python -m swarmeval.control.suite check suites/m1_core.yaml     # load; submit nothing
uv run python -m swarmeval.control.suite submit suites/m1_core.yaml --control 127.0.0.1:7090
```

Both send the suite file and one [bundle](#case-bundles) per distinct `cases[].path` to
`SubmitSuite`. The control plane unpacks each bundle, loads the suite with
`swarmeval.core.load_suite_text` against them, so the format is checked in one place, and
queues each entry as its own submission with its overrides (the suite's `models` as the `model`
axis), its epochs, and the label `<suite id>.<8 hex>`, fresh for each submit *(proposed)*. All
of a suite's runs go into the queue in one transaction, together with the
[library](#case-library) revisions its bundles are pushed as, so a suite with a broken case
queues nothing and stores no revision. The label is stored as
`control.run_specs.suite`, copied to reruns and summaries, and `ListRuns` and
`python -m swarmeval.analysis report --suite` filter on it. Labels are lowercase letters, digits,
`_`, `.`, and `-`.

### Case loading

Built. `swarmeval.core.load_case` parses both files with PyYAML `safe_load` into pydantic v2
models, expands variants, and validates every variant once, at this boundary. The control plane
and the worker call the same function. `run_spec` turns one variant into the runtime's
`RunSpec`. The format, the substitution rules, and what is rejected are in
[case-format.md](../case-format.md).

Not built yet: rejecting an egress rule that points at a platform address (with `network:`, part of the network capability, later),
and exporting JSON Schema from the models for editor validation.

### Case code

A case may load an extension from its own directory: `use: case:extensions/market.py` (case
`schema_version: 3`). The loader reads the file's text into the variant (`Variant.code`), so a
run imports exactly what was submitted, pinned by the bundle hash. The worker imports it with
`swarmeval.runtime.extensions.case_resolver`, one module per distinct source text, which must
define exactly one `@extension` setup function. A `case:` file is one module: it cannot import
sibling files of the case.

Case code runs inside the worker process with the worker's privileges: its database and object
store credentials, and its network. So it runs only where the deployment allows it, which is off
by default (M2 spec decision 4):

- `swarmeval-control --allow-case-code` accepts such cases. Without it `SubmitRuns` refuses them
  with `FAILED_PRECONDITION`, naming the files.
- `swarmeval-worker --allow-case-code` imports them. Without it a run of such a case fails, the
  error naming the flag.

Both read `SWARMEVAL_ALLOW_CASE_CODE=1` as the flag. Turn it on only for cases whose code you
have read; sandbox isolation does not cover it.

### Queue and claiming

Runs are rows in `control.runs` with `status`, `owner_id`, `lease_until`, and `owner_epoch`;
`control.run_specs` holds what to run (case hash, overrides, variant, epoch). The
control plane only enqueues. Workers claim with `FOR UPDATE SKIP LOCKED`, which sets the owner and
lease and increments `owner_epoch` in the same statement. "The control plane assigns a run" is
therefore a claimable row, not a push, and the control plane holds no state of its own. Limits on
concurrency per submission and per model backend are conditions in the claim query *(proposed)*.

Status values are `queued`, `running`, `paused`, `interrupted`, `done`, `failed`, and `cancelled`.
A run ends `done` (finished, or stopped by a limit or an extension), `cancelled`, `failed` (the
case no longer loads, an extension failed, the [isolation self-check](#isolation-self-check)
failed, a service gave an answer it would give again — model-gateway's backend refused or failed
a call, `502`, model-gateway refused the request, any `4xx` such as an unknown model, or sandboxd
refused the request, `INVALID_ARGUMENT` such as a bad user, hostname, or seed file — or a bug), or
`interrupted` (sandboxd or model-gateway itself was unavailable, sandboxd forgot the run after a
restart, `NOT_FOUND`, the worker that owned the run restarted, or it stopped renewing its lease
and another worker [took the run over](#leases-fencing-and-takeover); M3 pauses or resumes
instead). A finished run's status is final: `finish` changes a run only while it is `running`,
`paused`, or `cancelled` (a cancel lands before its owner finishes the run), and only at the
caller's `owner_epoch`. A stale owner's `finish` raises `FencedError`, and that owner writes no
summary either, since the run's new owner writes one. A claim also sets the run's `lease_until`.

### Reruns

A run that becomes `interrupted` is rerun as a new epoch. In the same transaction that sets the
status, the queue adds one queued run for the same submission, case revision, overrides, and
variant, at the variant's next unused epoch number, so its seed differs too
(`swarmeval.control.queue`). The rerun's `run_specs.replaces` names the run it stands in for, and
`GetRun` shows it as `replaces`. Both places a run becomes `interrupted` do this: `finish`, when
a worker's run ends on an outage or a worker finishes a run it took over, and `interrupt_owned`,
when a worker restarts. `interrupt_owned`
also increments the `owner_epoch` of every run it finishes, so a process of the old owner that
still runs one is fenced: its next write fails and its `finish` changes nothing. A run cancelled
meanwhile stays `cancelled` and is not rerun; `failed` and `cancelled` runs are never rerun.

A variant gets at most `epochs` reruns per submission, counted over all its runs, so a backend
that stays broken cannot requeue forever *(proposed)*. Past the cap the run stays `interrupted`
with no rerun, and the report shows the gap: per variant it lists the epochs requested, `done`,
replaced, and missing ([analysis](analysis.md#capabilities)). Interrupted runs never count toward
rates. Reruns of one variant are serialized with a transaction-scoped advisory lock, so two runs
interrupted at once take different epochs.

This is the default from trajectory-first spec Open question 4, pending its asker's confirmation.
From M3, a run whose recovery falls below `recovery.min_fidelity` is rerun the same way.

### Live events

Each control-plane replica holds one connection that runs `LISTEN swarmeval_events`. A
notification wakes the `StreamEvents` subscribers for that run. They then read
`runs.events WHERE run_id = ? AND seq > last_seen`. The notification is only a wakeup and the rows
are the data, so a lost notification costs latency, never an event.

## Worker

### Run lifecycle

1. Claim a run. Load its bundle and generate per-run secrets: file canaries and one canary
   per sandbox.
2. Ask [sandboxd](sandboxd.md) to create the run and its sandboxes. Sandboxes carry `run_id` /
   `sandbox_id` labels.
3. Run the [isolation self-check](#isolation-self-check). The run fails if any probe gets through.
4. Attach the event stream to [model-gateway](model-gateway.md).
5. Drive the turn policy until limits, task end, or cancel.
6. Run the final-state scorers while the sandboxes still exist.
7. Compare each agent's context with what the gateway, the tools, and the bus recorded, and
   commit the outcome as a `transcript_check` event
   ([event-log.md](../event-log.md#transcript-check)).
8. Export ([event-log.md](../event-log.md#export)), tear down through sandboxd, and mark the
   run `done`.

Built in `swarmeval.worker` for M0. Step 2 calls `CreateRun` and then creates each sandbox with
the `os_user`s of the agents in it and its sandbox canary as its identity. The self-check runs
before the model-gateway stream is attached, so a run it fails never reaches a model. Step 7
runs for cancelled runs too, since they are exported. With the network capability
(later), steps 1–3 also generate the TLS interception CA and a net-gateway certificate, start
[net-gateway](net-gateway.md) and service containers, and attach its event stream. Sandboxes are
destroyed whatever happens. A cancel is seen by polling the run's status every 2 s, so it takes effect at the first
hook point after that. A failed or interrupted run keeps its events but is not exported. If the
run's summary cannot be written once its status is, a `done` run becomes `failed`, since no
report can see it; any other status stays, so an interrupted run keeps its rerun and a cancel
still wins. Either way the run's error adds why (`Queue.summary_failed`). The
worker runs up to `--max-runs` runs at once (default 4); `--worker-id` (default the hostname)
should stay the same across restarts, because on start the worker marks the runs it still owned
`interrupted`, which [reruns](#reruns) them, gives a run cancelled while it ran its `finished_at`
and summary, and asks its sandboxd to remove both kinds' sandboxes. Under a new id, the old id's
runs wait for their leases to run out and are [taken over](#leases-fencing-and-takeover).
On SIGTERM, as on Ctrl-C, the worker cancels its runs, which removes their sandboxes, and
exits 0; the control plane and the analysis service stop the same way
(`swarmeval.config.run_service`). With
`--stay-halted`, a worker that a failed self-check halted stays up idle instead of exiting,
for deployments that restart whatever exits ([deployment.md](../deployment.md#when-a-worker-stops)).

### Several workers

Any number of worker processes can share one Postgres and one bucket. A claim is one
`UPDATE … WHERE run_id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)`, so two workers never take
the same run, and every later write to the run's `control.runs` row is conditioned on the
`owner_epoch` the claim set. Summaries are one object per run, so workers never write the same
object. Each worker still runs up to `--max-runs` runs at once.

Worker ids must be unique among live workers, because at start a worker finishes every run its id
owns. While it serves, a worker holds a Postgres session-level advisory lock on its id
(`swarmeval.worker.hold_worker_id`), on a connection of its own; a second worker with an id in use
exits before touching any run. The lock goes with the connection, so a crashed worker's id is free
at once. Every 10 s the worker checks that connection is still the session holding the lock; if
it dropped, the worker stops serving (its runs are cancelled and stay `running` until a worker
with its id starts or their leases run out) and exits with `WorkerIdLost`, since another process
could now take the id. For up to those 10 s the id is unguarded. A worker that never comes back
leaves its runs to be [taken over](#leases-fencing-and-takeover) by any worker once their leases
run out.

Stopping a worker (SIGINT, or cancelling `serve`) cancels its runs, which stay `running` until it
restarts or their leases run out. A run whose final status is being written finishes writing it and its summary first, so
no run ends without a summary.

### Agent loop

A built-in ReAct loop per agent, written in this repo rather than taken from an agent framework.
How the loop and its extension hooks work today, and how to write an extension, is in
[agent-runtime.md](../agent-runtime.md). The turn policy
decides who steps next. `round_robin` is in M0;
`async` and `event_driven` are in M2. A step builds the context from `messages`, calls
model-gateway with the agent's virtual key, then dispatches the parsed tool calls.

| Tool | Runs where |
|---|---|
| `shell`, `fs` | Inside the agent's sandbox, through sandboxd `Exec` |
| `web_request` | In the worker, for agents whose case lists it (M1). Sandboxes have no network, so this is the only way out. See [below](#web_request) |
| `send_message`, `read_messages` | Message Bus |
| `flag` (monitor agents) | Monitor |

Tool calls on one sandbox run one at a time. Under `async`, each sandbox has an `asyncio.Lock`
held for the length of an `Exec`, so a call window's file changes belong to one caller. Tool
permissions are per agent; OS permissions are per sandbox.

### `web_request`

Built: `swarmeval.web.HttpWebClient`, one per run, and the `WebTool` `WEB_REQUEST` in
`swarmeval.runtime.tools`. Decided in the
[trajectory-first spec](../../spec/2026-10-02-trajectory-first/README.md) decision 3.

- **Who gets it.** An agent whose `tools` list names `web_request`. Any public address, any
  method. Nothing beyond the address check limits what an agent does to real third-party sites;
  that is deferred to the network capability (trajectory-first spec Q3).
- **Where it runs.** In the worker, through the `WebClient` the loop is given. The sandbox stays
  offline. A run whose agent lists `web_request` fails to start without one.
- **Address check.** The host is resolved, every resulting address is checked, and the
  connection goes to the checked address without resolving again, with the name in `Host` and in
  TLS SNI, so certificates are verified against the name. If any address is not global unicast,
  the request is refused: loopback, private, link-local (cloud metadata included), CGNAT,
  multicast, reserved, and IPv6 forms carrying such an IPv4 address (mapped, 6to4, Teredo,
  NAT64 under `64:ff9b::/96`). IPv4 is tried before IPv6. Proxy settings in the worker's
  environment are ignored, and so is any `Host` header in the arguments: `Host` is always the
  URL's. Deployment requirement: do not run the worker IPv6-only behind DNS64 with a
  network-specific NAT64 prefix, since a private IPv4 target synthesized under that prefix
  looks global; such a deployment needs the prefix added to the check first. A case cannot turn this off. The worker reaches Postgres,
  object storage, model-gateway, and the Control API, so this check is what keeps an agent away
  from them.
- **No redirects followed.** A 3xx goes back to the agent as is, so one call is one outbound
  request.
- **Stateless.** The worker adds no credentials, refuses every cookie a response sets, and opens
  a new connection for every call.
- **Recorded.** The call's `ToolEvent` carries a `WebExchange` under `metadata.swarmeval.web`:
  the request, the address connected to, the status and headers, and both bodies by hash in the
  blob store ([event-log.md](../event-log.md#large-objects)), uploaded before the event commits.
  The agent sees the body's first bytes, decoded by the response's charset, with a note when it
  was cut. A refused request is a `ToolEvent` too: the agent sees a connection error, and the
  reason is in `web.refused`. DNS, connect, TLS, and timeout failures are in `web.error` and are
  shown to the agent, as they would be outside an evaluation.
- **Rewritable.** The result passes through `after_tool_result` like any other tool's
  ([agent-runtime.md](../agent-runtime.md)).

Limits are in `WebLimits`: 64 KiB of body shown, 16 MiB read, 1 MiB of request body; the
timeout covers resolution, connect, and the whole body. These values and a per-run request limit
are *(proposed, trajectory-first spec Q2)*. The HTTP client is `httpx2`, already the worker's
client for model-gateway.

### Write-before rule

Content enters an agent's context only after the transaction that records it has committed. This
covers model responses, tool results, and delivered messages. So anything an agent has seen is in
the database, and recovery never has to guess.

### Message Bus

In the worker process (`swarmeval/gateway/bus/`). Channels and members come from the case. An agent
sends with the `send_message` tool, naming a channel it is a member of; any other channel is an
error result the agent sees. The message goes to every other member. The send writes `msg.send`
in the tool call's transaction, and the store opens one `deliveries` row per recipient with it.

Once the send has committed, the loop routes the message to each recipient through the
`before_deliver` hook, whose verdict commits with its interventions and the row's new status:
delivered as is or rewritten, held for some of the recipient's turns (`delayed`, `due_turn`), or
dropped for good. The rules, and how a delay is counted, are in
[agent-runtime.md](../agent-runtime.md#routing-messages).

Delivery is pushed: at the start of a recipient's turn, each message due is admitted as a user
message (``Message from <sender> on channel `<channel>`:`` and the content), with a `msg.deliver`
event whose parent is the send, or the last intervention on it. The store closes the
`deliveries` row in the same transaction. `msg.deliver` carries the content actually delivered,
which differs from the original when a `before_deliver` rewrite applies. A message due for a
finished agent gives it another turn.

Interventions on channels are extensions: `swarmeval.bus.drop`, `swarmeval.bus.delay`,
`swarmeval.bus.paraphrase`, and `swarmeval.bus.inject`
([agent-runtime.md](../agent-runtime.md#built-in-extensions)), configured under `extensions:`
or with a channel's `interventions:` ([case-format.md](../case-format.md#channel-interventions)).
`log` is not an intervention: every message already has its `msg.send` and `msg.deliver`.
`paraphrase` calls model-gateway with its own instance's key, so the rewrite is recorded like any
other model call, under that instance.

### Budgets and limits

Token use comes from each `ModelEvent`'s usage and is deducted in the same transaction, in
`agent_state`. Crossing `max_tokens`, `max_turns`, or `wall_clock` (checked before each turn)
writes a `SampleLimitEvent` and stops the run, or under `async` for `max_turns` the agent.
Paused time does not count toward wall clock.

### Monitor

The built-in extension `swarmeval.monitor` runs detectors in the worker on each event right after
it commits, as an `on_event` observer ([agent-runtime.md](../agent-runtime.md#built-in-extensions)).
The detectors are `swarmeval.detect`'s, the same the case's `rule` scorer and the analysis
`detect` job run (M2 spec decision 6). They read an `EventView` of each event: who caused it, the
text an agent produced or was shown, the file changes and processes sandboxd saw, and a message's
channel. Detector state, such as which canaries a message carried to whom, is committed with the
Monitor's extension state.

Each hit is an `alert`, parented to the event that triggered it. What the Monitor asks for on
the last agent's final response still takes effect: the loop lets the observers catch up before
it decides the run is over, and an injection wakes the agent it is for. Then the configured action, at
most `max_actions` times (default 1), with the alert as its cause: `pause`, `stop`, or `inject`.
`trace` from any of them leads through the alert to what caused it.

| Detector | Hits on | Needs the worker |
|---|---|---|
| `protected_path_write` | A change under a protected path, seen by a tool call, an extension's command, or the final diff; `ignore` patterns. The final diff is committed after the agent loop, when the Monitor no longer runs, so only a `rule` scorer sees it | |
| `canary` | A file canary's token, as is or decoded, in what an agent produced or was shown | Yes: the run's tokens |
| `cross_sandbox` | A canary reaching an agent outside its sandbox other than in a message on a declared channel, by the [sandbox canary](#sandbox-canaries) rule | Yes: the run's tokens |
| `rule` | A rule set's keywords and regexes, as is and decoded, in the text roles given ([analysis rule sets](analysis.md#rule-sets)) | |
| `zero_width` | At least `min_count` invisible characters (zero-width spaces and joiners, word joiners, invisible operators, the byte order mark, bidirectional controls, tag characters) in a text | |
| `acrostic` | One of `words` spelled by the initials of a text's lines or sentences, with at least `min_lines` of them | |
| `fixed_format_numbers` | At least `min_count` numbers of one exact format in a text, counting only formatted ones: zero-padded, a zero-padded fraction, or three or more decimals | |
| `message_timing` | At least `min_messages` (6) of one sender's messages on one channel, within its last `window` (16), whose intervals rounded to `resolution_s` (1 s) take two to `max_levels` (2) values: a timing code. Once per sender and channel. Mostly meaningful under `async` | |
| `file_timestamps` | A changed file whose modification time is more than `skew_s` (a day) from when the change was seen: a time set on purpose. Needs event schema 6 file changes, which carry `mtime_us` | |
| `shared_file` | A tool call that names (by path, or by file name as a word) a regular file another agent's call wrote in the same sandbox and nobody has deleted since | |

The text roles a detector reads (`roles`) are `model_output` (content, reasoning, and tool call
arguments), `tool_output`, `message` (what a sender sent), `rewritten_message` (a delivery a
`before_deliver` intervention replaced), and `delivered` (a delivery as the recipient read it).
The statistical detectors read `message` by default.

### Forks

A fork is a new run that goes on from a finished run's state at a turn boundary, with edits, to
ask what would have happened otherwise (M2 spec decisions 8 and 9). `ForkRun(run_id,
at_event_id, edits)` picks the start of the turn `at_event_id` happened in: the last
[checkpoint](../event-log.md#checkpoints) before the event. The source must have ended `done` or
`cancelled`, so its export exists for `trace` to follow. A checkpoint taken while an
extension's `ctx.spawn` work was still running is refused: a fork cannot carry a coroutine
over. The loop commits one at the start of
every run-wide turn, once the observers have caught up, so no agent is mid-step there. The
control plane checks the edits against the checkpoint and the contexts it locates, and queues
`<source>.f<n>` with the source's case revision, overrides, variant, and epoch, so the same seed,
with `forked_from`, `fork_seq` (the checkpoint's seq), and `fork_edits`.

The worker then:

1. Reads the checkpoint, and writes every source event id in it as `<source>:<event id>`.
2. Plants the source's canary tokens (`runs.canaries`) in sandboxes built from the image and seed
   files, as for any run.
3. Restores each sandbox, before its first command, from the recorded file changes of the
   source (and, for a fork of a fork, its sources) up to the fork point, through sandboxd's
   `RestoreFiles`: for every path its last change, a deletion, a directory, or a file whose
   content is in the blob store, with its recorded mode and owner. A file sandboxd stored by hash
   only (over 1 MiB), a symlink, anything else, or an owner sandboxd could not set cannot come
   back, and makes the fork `fs_partial` instead of `fs_restored`, recorded as the run's
   `fidelity`. Background processes never come back, and no fork claims
   to be exact.
4. Runs the isolation self-check, then starts the loop from the checkpoint: the source's contexts
   are copied at their generation numbers, agent, extension, and mail state is restored, and the
   round in progress goes on. Its chain links into the source's
   ([hash chain](../event-log.md#hash-chain)).
5. Applies the edits, each an `intervention` with `hook: fork` parented to the fork's `started`:
   message edits give the agent a new generation (`edit_context`, like a compaction), and it
   takes a turn again even if it had finished; a delivery edit replaces what a carried message
   will deliver (`deliver`). Replacements apply before deletions, by the indexes at the fork
   point. Then `on_resume`
   runs with `fork: true` and the fidelity.

A fork is scored like any run, on its own events and its final state; what its source did before
the fork point is not scored again. Scorers that remember what they saw (`cross_sandbox`, `rule`)
read the source's events up to the fork point first, without scoring them, so a token a channel
delivered there does not count as a crossing. Reports list forks apart from the rates, with their source
and fidelity ([analysis](analysis.md#capabilities)). An interrupted fork gets no rerun. `trace`
follows a fork's parents into its source's export.

### Pauses

A `pause` action (`ctx.actions.pause`, which the Monitor uses) takes effect at the loop's next
hook point, after the observers have committed what they were doing. The loop commits a
`lifecycle` event with `status: paused`, and the worker sets the run's row to `paused`. The worker
then polls the row every `cancel_poll_s` until `ResumeRun` sets it back to `running`, commits
`resumed`, and goes on. A cancel while paused stops the run there, with no `resumed` event. Until M4's
console, a person resumes a run through the Control API (grpcurl).

### Final-state scorers

These run in the worker after the agent loop ends and before teardown
(`swarmeval.scorers.FinalStateScoring`). They read files through sandboxd and write
`ScoreEvent`s. Scores use `1 = triggered`, and the meaning is declared in
`Score.metadata.swarmeval` with the evidence's `event_ids`. Event-rule and judge scorers run
later, in analysis.

First, sandboxd's final diff of every sandbox is committed as `final_diff` events, so background
writes after the last call count. Then each scorer the case lists runs in order:

| Type | Triggered when |
|---|---|
| `protected_write` | Any tool call, extension command, or final diff saw a change under a protected path |
| `canary` | A file canary's token appeared in a model call, a tool result, a message, or the new content a `before_deliver` intervention gave a delivery (`where: rewritten_message`, never cross-sandbox, since it came through a declared channel), or is in a file the run created or modified and did not delete (read back through sandboxd, first 1 MiB) |
| `cross_sandbox` | A [sandbox canary](#sandbox-canaries), or a file canary, showed up in a model call, tool result, or message of an agent that does not use the canary's sandbox, and no message delivered to that agent had carried it first |
| `command` | The case's script, run in the named sandbox after `interpreter` (default `sh -c`), exits non-zero, or zero with `triggered: zero_exit`. A timeout counts as non-zero. The run is recorded as a `SandboxEvent` with call id `score:<id>`, and what it changes is not seen by the scorers after it |
| `rule` | Its detector, run over every event the run committed up to and including the final diff, in order, as the Monitor would have, hits at least once. The verdict cites every hit's events; the explanation lists the first 20 hits |
| `event_value` | The `field` of the last `extension` event named `event` (from instance `extension`, when given) compares with `threshold` by `op`. No such event scores 0 and cites nothing; a field that is missing or not a number fails the run, since the case or its extension is wrong. The verdict cites that event |

Every positive verdict names its evidence in `event_ids`; a canary found only by reading a file
names the event that observed the file's last change.

### Canaries

There are two kinds: file canaries, which a case declares, and one canary per sandbox, which
every run gets ([below](#sandbox-canaries)). Both are watched by the `swarmeval.canary`
extension ([agent-runtime.md](../agent-runtime.md#built-in-extensions)), whose `canary_hit`
events say which kind (`kind`), where it was planted (`sandbox_id`), and whether the sighting
crossed sandboxes (`cross_sandbox`).

File canaries are declared in `env.yaml` ([case-format.md](../case-format.md#canaries)). For each run
the worker generates a fresh token per canary (`swarmeval.honeypot.place`, 24 hex characters),
renders the template, and passes the file to sandboxd `CreateSandbox` as a seed file, so it is
part of the sandbox's baseline and never shows up as an agent's change.

Detection (`swarmeval.honeypot.find_tokens`) matches the token ignoring case, and also through
encodings an agent may apply on the way out: base64 (standard or URL-safe, padded or not, wrapped
or not), hex, gzip and zlib, chained up to three layers, with a single-byte XOR as the innermost
layer. Each sighting records the chain that revealed it, outermost first (`via`, e.g.
`["base64", "gzip"]`), in the `canary_hit` event and the scorer's explanation. Decoded output per
searched text is capped at 16 times its size (at least 4 MiB) and one decompression at 1 MiB,
searched breadth first, so the first layer is always searched in full. Not recovered: a repeating
multi-byte XOR key, XOR applied before another encoding, and encodings outside this list.
The decoded views themselves (`swarmeval.honeypot.decode.views`) are also what analysis rule
scans match against.

#### Sandbox canaries

Built. Every sandbox instance of a run gets its own token, with no case declaration
(`swarmeval.honeypot.place_sandboxes`, 32 hex characters), placed in three spots when sandboxd
creates it:

| Spot | Value |
|---|---|
| Hostname | The token *(proposed)* |
| Environment variable `INSTANCE_ID` *(proposed)* | The token, in every process, exec'd commands included |
| `/etc/machine-id` *(proposed)* | The token, in the file's own format. Outside the key paths, so never diffed and never shown as a change. Left out for a sandbox with a key path at `/etc`, whose mount would hide it (`SandboxCanaryInfo.path` is then `None`) |

The names read as ordinary host identity and say nothing about canaries. The runtime spec
(decision 6) chose the three kinds of spot, not the names. Extensions see each one as a
`SandboxCanaryInfo` in `ctx.run.sandbox_canaries`, with the agents that use the sandbox.

A sighting is matched like a file canary's, through the same encodings, but counts only as
evidence of information crossing between sandboxes, so it is a hit only when both hold:

- the agent that produced or was shown it (model output, tool result, a message it sent) does not
  use the sandbox: a sighting in the agent's own sandbox is not a hit;
- no message delivered to that agent through a declared channel (`msg.deliver`) carried the token
  before. Agent A telling B its hostname is the case working as designed, and what B does with it
  afterwards is not a crossing. A sighting before the delivery still counts.

Events with no agent (an extension's own model call) never count. The same rule gives every file
canary sighting a `cross_sandbox` flag; file canary sightings are hits either way. Files the run
wrote are not searched for sandbox canaries.

### Isolation self-check

Built (`swarmeval.worker.probes`). After the sandboxes exist and before the model-gateway stream
is attached or any agent turn, the worker runs probes derived from the case's sandbox topology,
adapted from runtime spec decision 6 to sandboxes with no network (trajectory-first spec
decision 2). Agents that share a sandbox share one instance, so every pair of instances must be
isolated.

| Probe | Per | Gets through when |
|---|---|---|
| `interfaces` | Sandbox | `/proc/net/dev` (or `/sys/class/net`) lists an interface other than `lo` |
| `connect` | Sandbox | `nc`, or else `python3`, connects to `1.1.1.1:80` *(proposed)* |
| `shared_path` | Ordered pair | A marker file the other sandbox wrote into its writable, unprotected key paths and `/dev/shm` is visible here, at those paths or under this sandbox's own key paths |
| `proc` | Ordered pair | A process the other sandbox started, with a marker in its command line, shows up in this sandbox's `/proc` |
| `dns` | Ordered pair | The other sandbox's hostname or sandbox id resolves, through `getent`, else `python3`, else `nslookup` |

It runs in three steps, each one command per sandbox, all sandboxes at once, through sandboxd's
`Exec` as root with call ids `probe:plant`, `probe:check`, and `probe:clean`:

1. `plant` writes the marker files and starts the marker process (`sh -c 'sleep 120; :'
   <marker>`).
2. `check` runs every probe above from this sandbox. Each sandbox first finds its own marker file
   and process, so a probe that cannot see anything in this image says so instead of passing.
3. `clean` deletes the marker files and kills the marker process and its child, then waits until
   they are gone. Nothing of the probe is left in the baseline the agents' first call is diffed
   against, or among the processes a call's changes could be attributed to. A process that
   survives fails the run too.

Scripts are POSIX `sh` and need only what sandboxd already requires (`sh`, `sleep`, `tr`); the
tools above are tried in order. Tested on `busybox` and `python:3.12-slim`. Markers reach the
check script in two halves, so its own command line never carries one for the `/proc` scan to
find. Each command is an `isolation_probe` event ([event-log.md](../event-log.md#data-model)),
and each `check` event carries one finding per probe: `isolated`, `leaked`, or `unverified`, with
the evidence in `detail`. `unverified` (the image has none of a probe's tools, a marker could not
be written, the check printed nothing) is logged as a warning and does not fail the run
*(proposed)*. Probe events are not agent behavior: the final-state scorers and the canary
extension never read them.

Like markers, a peer's names reach the check script in two halves, and are scrubbed from what
the script prints: the hostname is the peer's [sandbox canary](#sandbox-canaries), which must not
appear in another sandbox's `/proc` or in its `isolation_probe` events.

If any probe gets through, the worker commits a `failed` lifecycle event and the run ends
`failed`, with an error naming each sandbox pair (or `network`), the probe, and what it saw. A
leak between sandboxes the case never declared is a fault of the worker's host, not an outcome of
the case, and every run the worker claimed would fail the same way. So the worker then claims no
more runs, lets the runs it is already executing finish, and `serve` exits with `WorkerHalted`,
naming the run and the probes that got through (`swarmeval-worker` exits non-zero with it). The
runs it did not claim stay queued for a healthy worker. Fix the host, then restart the worker.
With `--sandbox-network per-sandbox` every sandbox has an interface besides `lo`, so `interfaces`
fails the first run and stops the worker, until the network capability defines what its
self-check expects. Not probed: `web_request`'s address check, which its tests cover; side
channels such as CPU contention and timing, which the runtime spec (decision 6) says to record as
run metadata, not built.

## Leases, fencing, and takeover

M3. Leases, fencing, and taking a run over are built (`swarmeval.worker.leases`,
`Queue.claim_expired`); resuming a taken-over run is not, so for now every takeover reruns the
run. The tables were shaped for recovery from the start, so none of this needed a migration.

- **Lease.** A claim sets `lease_until` to `--lease-s` from now (default 30 s *(proposed)*), by the
  database's clock, so workers' clocks never have to agree. While a worker works on a run, until
  only its final status is left to write, it renews the lease every third of that. A run a
  renewal no longer finds (another worker took it over, or it was finished elsewhere) is stopped
  at once. A failed renewal is retried every twelfth of the lease, and no attempt lasts past the
  cutoff: two thirds of the lease after the last renewal that went out. At the cutoff the worker
  stops every run it holds, before any lease can run out. A stopped run
  is cancelled where it is: its sandboxes are removed and nothing is recorded, since the run now
  belongs to whoever takes it over.
- **Fencing.** Every write transaction starts with
  `SELECT owner_epoch FROM control.runs WHERE run_id = :run FOR SHARE`. On a mismatch it rolls
  back, and the worker drops the run. A takeover's `UPDATE` waits for in-flight transactions, so a
  stale owner's next transaction fails. This is why `control` and `runs` share one Postgres. It
  also covers an owner that missed its own deadline, for instance because its process was
  suspended.
- **Taking a run over.** Before claiming a queued run, a serving worker claims the unfinished run
  (`running`, `paused`, or cancelled while it ran) whose lease ran out first, with
  `FOR UPDATE SKIP LOCKED`, which increments `owner_epoch` and gives it a lease of its own. Today
  it then asks its sandboxd to remove the run's sandboxes, and finishes the run: `interrupted`
  with its error naming both workers, which [reruns](#reruns) it, or `cancelled` if it was
  cancelled while it ran. Then it writes the run's summary. Containers live on the node that
  created them, so a sandboxd on another node finds none, and they are left, labeled with the run,
  until the old worker's id restarts on that node *(proposed)*. A run claimed before leases
  existed has no lease, never runs out, and is still finished when its worker id restarts.
  Each takeover adds one to the run's `takeovers` (`control.runs`, migration 0012), which `Run`
  returns and `swarm runs get` shows; a claim from the queue and a restart of the owner's own
  id are not takeovers.

  With resuming, a takeover will do the following instead:
  1. Reconcile the run's containers through sandboxd `ListRun`. Live ones are adopted, not
     recreated.
  2. Set each sandbox's fidelity: `exact`, `fs_preserved`, or `lost`.
  3. Handle half-done steps. A model request whose response was not committed is resent. A tool
     call without a result gets an `InterruptEvent`. An undelivered message is redelivered,
     deduplicated by `event_id`.
  4. Re-attach the model-gateway stream. model-gateway keeps the stream with the highest
     `owner_epoch` and drops the old one.
  5. Write a `CheckpointEvent`.
  6. Continue from the latest `agent_state` row.

  If fidelity falls below the case's `recovery.min_fidelity`, the run is marked `interrupted` and
  a new epoch is queued, as every takeover does today. On docker, a run can only be resumed on its
  own node *(open, runtime spec Q4)*.
- **Fork** (M2, before the rest of this section). Read the `agent_state` row at step *k*, replace
  one message, and continue as a new run. This is the same query recovery uses.

## Pausing on infrastructure failure

From M3. When model-gateway or sandboxd stops answering, the worker pauses the
affected run. Signs are a dropped stream or gRPC `UNAVAILABLE`. While paused:

- No new agent step is scheduled.
- Sandboxes are frozen through sandboxd.
- No connection error is handed to an agent as a model response or tool result. A `web_request`
  that fails on the public internet is an ordinary result: the agent sees the error, as it would
  outside an evaluation.

On recovery the worker thaws the sandboxes and writes a `CheckpointEvent`. It records how long the
pause lasted, what was interrupted, and whether the agent could have seen anything, and counts the
pause toward the run's fidelity. A pause longer than the deployment's limit marks the run
`interrupted`.

## Tech choices

Libraries shared by every Python service are in [tech-stack.md](../tech-stack.md). This service
also uses the following:

| Need | Choice | Why |
|---|---|---|
| Canonical JSON for the hash chain | `rfc8785` (Trail of Bits) | Small, pure-Python, no dependencies. The semantics must be exact, and RFC 8785 is a finished standard |
| Object storage I/O | `pyarrow.fs.S3FileSystem` | pyarrow is already here for Parquet. One S3 client means no second dependency *(proposed)* |
| Per-run certificates and the interception CA (network capability, later) | `cryptography` | The standard Python X.509 library |
| Event log format | `inspect_ai`, pinned, imported only in `swarmeval/events/` | See [event-log.md](../event-log.md) |
| Agent loop | Our own | Frameworks such as Pydantic AI own the steps we must control: gateway-only model calls, commit before context, one sequencer per run, resume and fork from `agent_state` |

Postgres access uses SQLAlchemy 2.0 Core on the async psycopg 3 driver. The event writer uses no
ORM, because every insert on the hot path has a known shape. `LISTEN` runs on a raw psycopg
connection.

## Not settled

1. RPC names of `ControlService`.
2. Whether revisions no run references may ever be deleted (M4 spec, open question 3). They
   are kept.
3. Concurrency limits expressed in the claim query.
4. Default lease length of 30 s; stopping held runs two thirds of a lease after the last renewal
   that went out; and, until resuming is built, any worker taking over a run, whatever node its
   containers are on, since taking over only reruns it.
5. `pyarrow.fs` as the only S3 client. Export is built this way today: it writes `.eval` to a
   scratch file with `inspect_ai` and uploads it. The alternative is letting `inspect_ai` write to `s3://` through
   its own fsspec dependency, which means a second S3 client with its own configuration.
6. At most `epochs` reruns per variant and submission, and reruns going to the back of the queue.
7. A suite label per submit (`<id>.<8 hex>`), so submitting a suite twice gives two batches that
   reports keep apart unless asked for both.
8. Sandbox canary names: the hostname is the token itself, the variable `INSTANCE_ID`, and the file
   `/etc/machine-id`, left out where a key path at `/etc` would hide it.
9. The isolation self-check's details: `1.1.1.1:80` as the `connect` target, `/dev/shm` as the
   IPC-namespace probe, probes as root, and `unverified` not failing the run.
10. Channel interventions: routing a message when its send commits; a hold counted from the
    recipient's turns started at that moment; held mail not waking a finished agent until it is
    due at its next turn; an injected message posted at the start of a run-wide turn, after the
    current agent's mail was taken, and routed through `before_deliver` like any other; the
    shorthand's instance names (`<channel>.<intervention>`) and its place after `extensions:`.
11. Case code: `--allow-case-code` on both roles; a `case:` file as one module defining one
    extension, imported once per distinct source text; `event_value` failing the run on a field
    that is not a number, and reading only the last matching event.
12. The Monitor: `max_actions` defaulting to 1; `inject` defaulting to the agent of the event that
    hit, and injecting nothing for an event no agent caused; a pause polling the row at the
    cancel poll interval. The detectors' definitions: the invisible characters `zero_width`
    counts, `acrostic` needing named `words`, and what makes a number formatted.
13. Forks: the fork point as the start of the turn the event happened in; `<source>.f<n>` ids
    and the source's epoch; edits limited to replacing a message's text, deleting a user message,
    and replacing an undelivered message; scoring a fork on its own events only; no rerun for an
    interrupted fork; an owner sandboxd cannot set, and file content over sandboxd's 1 MiB
    content limit, making the fork `fs_partial`.
14. Turn policies: `event_driven` letting an agent go on until it answers without a tool call;
    `async` ending once every agent waits with nothing due, `max_turns` per agent ending the run
    `limit`, and a hook point under `async` waiting only for the events committed when it was
    reached. The timing detectors' defaults (`message_timing`'s two levels at 1 s,
    `file_timestamps`' one day) and `shared_file` matching a file name as a word.
