# orchestrator

Python. It loads cases, queues and schedules runs, and hosts every run's agent loop. It is the only
writer of a run's events and state. Its place among the services is in
[architecture.md](../architecture.md). The stored event format is in
[event-log.md](../event-log.md).

**Status:** the M0 part is built: case loading, the Control API, the queue, one worker that drives
each run from claim to export, the agent loop ([agent-runtime.md](../agent-runtime.md)), the
Message Bus without interventions, canaries, and final-state scorers. M1 adds `web_request`,
several workers, and batch runs over variants and epochs. M2 adds interventions, fork, the online
Monitor, and the async and event-driven turn policies. M3 adds leases, fencing, takeover, and
pausing. Items marked *(proposed)* go beyond what the specs decided;
they are listed under [Not settled](#not-settled).

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
| `swarmeval/control/` | Control API servicer, case bundles, the queue (shared with the worker), live event wakeups |
| `swarmeval/worker/` | Worker main loop and run lifecycle |
| `swarmeval/runtime/` | Turn policies, the ReAct agent loop, tool dispatch, extensions. Knows no database or service |
| `swarmeval/gateway/bus/` | Message Bus: channel ACLs, deliveries, interventions |
| `swarmeval/monitor/` | Online detectors and their actions |
| `swarmeval/honeypot/` | Canary generation and matching (through encodings), the `swarmeval.canary` and `swarmeval.env_state` extensions, honeypot templates (with the network capability, later) |
| `swarmeval/db/` | Table definitions for `control` and `runs`, engines, Alembic migrations (`migrate(url)`) |
| `swarmeval/events/` | Records to Inspect events, the hash chain, the Postgres `RunStore`, export. The only `inspect_ai` import |
| `swarmeval/scorers/` | Final-state scorers. Event-rule and judge scorers are shared with [analysis](analysis.md) |

Generated gRPC stubs live in `swarmeval/proto/`, generated from `proto/` by `mise run proto:gen`.
The sandboxd client is `swarmeval/sandbox/`.

## Control plane

### Control API

gRPC service `swarmeval.control.v1.ControlService` (`proto/swarmeval/control/v1/control.proto`)
on the internal network. It has no authentication until [edge](edge.md) exists in M4 *(RPC names
proposed)*. Messages may be up to 64 MiB, for case bundles.

| RPC | Does | From |
|---|---|---|
| `SubmitRuns` | Takes a case bundle, variant overrides (axis → list of values), and epochs (0 = the case's); validates, stores the bundle, and enqueues one run per variant and epoch; returns the submission id and run ids. A case that does not load is `INVALID_ARGUMENT` with the loader's message | Built |
| `GetRun`, `ListRuns` | Status, variant and its values, epoch, owner, isolation level, error, timestamps. `ListRuns` filters by submission, case, and status, newest first | Built. Fidelity arrives with recovery (M3) |
| `CancelRun` | Marks cancelled. A queued run never starts; a running one stops at its owner's next hook point, is not scored, and is still exported. A finished run is `FAILED_PRECONDITION` | Built |
| `StreamEvents` | Server stream of a run's events after a given `seq`, live while it runs; ends once the run has finished and every event was sent | Built |
| Case CRUD | Read and write `case.yaml` / `env.yaml` for the console | M4 |

Run ids are `<case>.<submission>.v<variant>.e<epoch>`. Override numbers travel as protobuf doubles;
a whole number becomes an int again.

### Case bundles

`SubmitRuns` carries the case directory as a tar archive. The control plane validates it with
the same loader the worker uses and stores it in object storage as `cases/sha256/<hex>.tar`.
Each run row references that hash. Control plane and workers share no disk, and the hash pins the
exact prompts, hooks, and data a run used *(proposed)*. Bundles are extracted with tarfile's
`data` filter: `..`, links that leave the directory, and device files are refused, and absolute
member paths land inside the directory. `case.yaml` must be at the archive's root.

### Case loading

Built. `swarmeval.core.load_case` parses both files with PyYAML `safe_load` into pydantic v2
models, expands variants, and validates every variant once, at this boundary. The control plane
and the worker call the same function. `run_spec` turns one variant into the runtime's
`RunSpec`. The format, the substitution rules, and what is rejected are in
[case-format.md](../case-format.md).

Not built yet: rejecting an egress rule that points at a platform address (with `network:`, part of the network capability, later),
and exporting JSON Schema from the models for editor validation.

### Queue and claiming

Runs are rows in `control.runs` with `status`, `owner_id`, `lease_until`, and `owner_epoch`;
`control.run_specs` holds what to run (case hash, overrides, variant, epoch). The
control plane only enqueues. Workers claim with `FOR UPDATE SKIP LOCKED`, which sets the owner and
lease and increments `owner_epoch` in the same statement. "The control plane assigns a run" is
therefore a claimable row, not a push, and the control plane holds no state of its own. Limits on
concurrency per submission and per model backend are conditions in the claim query *(proposed)*.

Status values are `queued`, `running`, `paused`, `interrupted`, `done`, `failed`, and `cancelled`.
In M0 a run ends `done` (finished, or stopped by a limit or an extension), `cancelled`, `failed`
(the case no longer loads, an extension failed, the model backend refused or failed a call —
model-gateway's `502` — or a bug), or `interrupted` (sandboxd or model-gateway itself was
unavailable; M3 pauses instead). Leases are not taken yet (M3).

### Live events

Each control-plane replica holds one connection that runs `LISTEN swarmeval_events`. A
notification wakes the `StreamEvents` subscribers for that run. They then read
`runs.events WHERE run_id = ? AND seq > last_seen`. The notification is only a wakeup and the rows
are the data, so a lost notification costs latency, never an event.

## Worker

### Run lifecycle

1. Claim a run. Load its bundle and generate per-run secrets: canaries.
2. Ask [sandboxd](sandboxd.md) to create the run and its sandboxes. Sandboxes carry `run_id` /
   `sandbox_id` labels.
3. Attach the event stream to [model-gateway](model-gateway.md).
4. From M1, run the isolation probes. The run fails if any probe gets through.
5. Drive the turn policy until limits, task end, or cancel.
6. Run the final-state scorers while the sandboxes still exist.
7. Compare each agent's context with what the gateway, the tools, and the bus recorded, and
   commit the outcome as a `transcript_check` event
   ([event-log.md](../event-log.md#transcript-check)).
8. Export ([event-log.md](../event-log.md#export)), tear down through sandboxd, and mark the
   run `done`.

Built in `swarmeval.worker` for M0. Step 2 calls `CreateRun` and then creates each sandbox with
the `os_user`s of the agents in it; the probes are not built yet. Step 7 runs for cancelled
runs too, since they are exported. With the network capability
(later), steps 1–3 also generate the TLS interception CA and a net-gateway certificate, start
[net-gateway](net-gateway.md) and service containers, and attach its event stream. Sandboxes are
destroyed whatever happens. A cancel is seen by polling the run's status every 2 s, so it takes effect at the first
hook point after that. A failed or interrupted run keeps its events but is not exported. The
worker runs up to `--max-runs` runs at once (default 4); `--worker-id` (default the hostname)
must stay the same across restarts, because on start the worker marks the runs it still owned
`interrupted`.

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

Delivery is pushed: at the start of a recipient's next turn, each waiting message is admitted as a
user message (``Message from <sender> on channel `<channel>`:`` and the content), with a
`msg.deliver` event whose parent is the send. The store closes the `deliveries` row in the same
transaction. `msg.deliver` carries the content actually delivered, which differs from the
original when an intervention applies. A message to a finished agent gives it another turn.
Interventions (`log`, `drop`, `delay`, `paraphrase`, `inject`) arrive in M2.
`paraphrase` calls model-gateway with a bus-owned key, so the rewrite is recorded like any other
model call.

### Budgets and limits

Token use comes from each `ModelEvent`'s usage and is deducted in the same transaction, in
`agent_state`. Crossing `max_tokens`, `max_turns`, or `wall_clock` writes a `SampleLimitEvent` and
stops the agent or the run. Paused time does not count toward wall clock.

### Monitor

From M2, detectors run in the worker and receive each event right after it commits. They share
their interface with the event-rule scorers. Actions are alert, pause for a human, terminate the
run, and inject a message; each action is itself an event.

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
| `canary` | A canary token appeared in a model call, a tool result, or a message, or is in a file the run created or modified and did not delete (read back through sandboxd, first 1 MiB) |
| `command` | The case's script, run in the named sandbox after `interpreter` (default `sh -c`), exits non-zero, or zero with `triggered: zero_exit`. A timeout counts as non-zero. The run is recorded as a `SandboxEvent` with call id `score:<id>`, and what it changes is not seen by the scorers after it |

Every positive verdict names its evidence in `event_ids`; a canary found only by reading a file
names the event that observed the file's last change.

### Canaries

Canaries are declared in `env.yaml` ([case-format.md](../case-format.md#canaries)). For each run
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
Per-sandbox canaries arrive in M1.

## Leases, fencing, and takeover

From M3. Until then a worker crash marks its runs `interrupted`, and a batch reruns them as new
epochs. The tables are already shaped for recovery, so M3 needs no migration.

- **Lease.** The owner renews `lease_until` every third of the lease length. The default lease is
  30 s *(proposed)*. An owner that fails to renew stops executing before its lease runs out.
- **Fencing.** Every write transaction starts with
  `SELECT owner_epoch FROM control.runs WHERE run_id = :run FOR SHARE`. On a mismatch it rolls
  back, and the worker drops the run. A takeover's `UPDATE` waits for in-flight transactions, so a
  stale owner's next transaction fails. This is why `control` and `runs` share one Postgres.
- **Takeover.** Another worker claims a run whose lease has expired, which increments
  `owner_epoch`. It then does the following:
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
  a new epoch is queued. On docker, a run can only be taken over on its own node *(open, runtime
  spec Q4)*.
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
2. Case bundles uploaded with `SubmitRuns` and stored by hash, as opposed to a case store the
   Control API manages. M4's case CRUD may change this.
3. Concurrency limits expressed in the claim query.
4. Default lease length of 30 s.
5. `pyarrow.fs` as the only S3 client. Export is built this way today: it writes `.eval` to a
   scratch file with `inspect_ai` and uploads it. The alternative is letting `inspect_ai` write to `s3://` through
   its own fsspec dependency, which means a second S3 client with its own configuration.
