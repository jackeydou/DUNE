# orchestrator

Python. It loads cases, queues and schedules runs, and hosts every run's agent loop. It is the only
writer of a run's events and state. Its place among the services is in
[architecture.md](../architecture.md). The stored event format is in
[event-log.md](../event-log.md).

**Status:** the M0 part is built: case loading, the Control API, the queue, one worker that drives
each run from claim to export, the agent loop ([agent-runtime.md](../agent-runtime.md)), the
Message Bus without interventions, canaries, and final-state scorers. M2 adds several workers,
leases, fencing, takeover, and pausing. M3 adds interventions, fork, the online Monitor, and the
async and event-driven turn policies. Items marked *(proposed)* go beyond what the specs decided;
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
| `swarmeval/honeypot/` | Canary generation and matching (decoding from M1), the `swarmeval.canary` and `swarmeval.env_state` extensions, honeypot templates (M1) |
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
| `GetRun`, `ListRuns` | Status, variant and its values, epoch, owner, isolation level, error, timestamps. `ListRuns` filters by submission, case, and status, newest first | Built. Fidelity arrives with recovery (M2) |
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

Not built yet: rejecting an egress rule that points at a platform address (with `network:`, M1),
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
unavailable; M2 pauses instead). Leases are not taken yet (M2).

### Live events

Each control-plane replica holds one connection that runs `LISTEN swarmeval_events`. A
notification wakes the `StreamEvents` subscribers for that run. They then read
`runs.events WHERE run_id = ? AND seq > last_seen`. The notification is only a wakeup and the rows
are the data, so a lost notification costs latency, never an event.

## Worker

### Run lifecycle

1. Claim a run. Load its bundle and generate per-run secrets: canaries, the TLS interception CA,
   and the net-gateway certificate (the last two from M1).
2. Ask [sandboxd](sandboxd.md) to create the run: networks, [net-gateway](net-gateway.md),
   services, and sandboxes. Sandboxes and service containers carry `run_id` / `sandbox_id` labels.
3. Attach the event streams to [model-gateway](model-gateway.md) and, from M1, to net-gateway.
4. From M1, run the isolation probes. The run fails if any probe gets through.
5. Drive the turn policy until limits, task end, or cancel.
6. Run the final-state scorers while the sandboxes still exist.
7. Export ([event-log.md](../event-log.md#export)), tear down through sandboxd, and mark the
   run `done`.

Built in `swarmeval.worker` for M0. Of the M1 steps, step 2 creates the networks (`CreateRun`)
and then each sandbox with the `os_user`s of the agents in it; net-gateway, services, certificates,
and probes are not built yet. Sandboxes are destroyed whatever
happens. A cancel is seen by polling the run's status every 2 s, so it takes effect at the first
hook point after that. A failed or interrupted run keeps its events but is not exported. The
worker runs up to `--max-runs` runs at once (default 4); `--worker-id` (default the hostname)
must stay the same across restarts, because on start the worker marks the runs it still owned
`interrupted`.

### Agent loop

A built-in ReAct loop per agent, written in this repo rather than taken from an agent framework.
How the loop and its extension hooks work today, and how to write an extension, is in
[agent-runtime.md](../agent-runtime.md). The turn policy
decides who steps next. `round_robin` is in M0;
`async` and `event_driven` are in M3. A step builds the context from `messages`, calls
model-gateway with the agent's virtual key, then dispatches the parsed tool calls.

| Tool | Runs where |
|---|---|
| `shell`, `fs` | Inside the agent's sandbox, through sandboxd `Exec` |
| `http` | Inside the sandbox as well, so the request crosses net-gateway like any other traffic. The worker never makes network requests on an agent's behalf |
| `send_message`, `read_messages` | Message Bus |
| `flag` (monitor agents) | Monitor |

Tool calls on one sandbox run one at a time. Under `async`, each sandbox has an `asyncio.Lock`
held for the length of an `Exec`, so a call window's file changes belong to one caller. Tool
permissions are per agent; OS permissions are per sandbox.

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
Interventions (`log`, `drop`, `delay`, `paraphrase`, `inject`) arrive in M3.
`paraphrase` calls model-gateway with a bus-owned key, so the rewrite is recorded like any other
model call.

### Budgets and limits

Token use comes from each `ModelEvent`'s usage and is deducted in the same transaction, in
`agent_state`. Crossing `max_tokens`, `max_turns`, or `wall_clock` writes a `SampleLimitEvent` and
stops the agent or the run. Paused time does not count toward wall clock.

### Monitor

From M3, detectors run in the worker and receive each event right after it commits. They share
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
part of the sandbox's baseline and never shows up as an agent's change. Detection is a plain
substring match; decoding (base64, gzip, XOR) and per-sandbox canaries arrive in M1.

## Leases, fencing, and takeover

From M2. In M0 a worker crash marks its runs `interrupted`. The tables are already shaped for
recovery, so M2 needs no migration.

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
  4. Re-attach the gateway streams. The gateways keep the stream with the highest `owner_epoch`
     and drop the old one.
  5. Write a `CheckpointEvent`.
  6. Continue from the latest `agent_state` row.

  If fidelity falls below the case's `recovery.min_fidelity`, the run is marked `interrupted` and
  a new epoch is queued. On docker, a run can only be taken over on its own node *(open, runtime
  spec Q4)*.
- **Fork.** Read the `agent_state` row at step *k*, replace one message, and continue as a new
  run. This is the same query recovery uses.

## Pausing on infrastructure failure

From M2. When model-gateway, net-gateway, or sandboxd stops answering, the worker pauses the
affected run. Signs are a dropped stream or gRPC `UNAVAILABLE`. While paused:

- No new agent step is scheduled.
- Sandboxes are frozen through sandboxd.
- No connection error is handed to an agent as a model response, tool result, or network response.

On recovery the worker thaws the sandboxes and writes a `CheckpointEvent`. It records how long the
pause lasted, what was interrupted, and whether the agent could have seen anything, and counts the
pause toward the run's fidelity. A pause longer than the deployment's limit marks the run
`interrupted`. What happens between a worker dying and its lease expiring is *(open, runtime spec
Q13)*.

## Tech choices

Libraries shared by every Python service are in [tech-stack.md](../tech-stack.md). This service
also uses the following:

| Need | Choice | Why |
|---|---|---|
| Canonical JSON for the hash chain | `rfc8785` (Trail of Bits) | Small, pure-Python, no dependencies. The semantics must be exact, and RFC 8785 is a finished standard |
| Object storage I/O | `pyarrow.fs.S3FileSystem` | pyarrow is already here for Parquet. One S3 client means no second dependency *(proposed)* |
| Per-run certificates and the interception CA (M1) | `cryptography` | The standard Python X.509 library |
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
