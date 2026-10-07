# Event log

The event log is the evidence record of a run: what was stored, in what format, how it is chained,
and how it leaves the database. Its code is `swarmeval/events/`, inside the
[orchestrator](services/orchestrator.md). That package is the only place that imports
`inspect_ai`. Where events come from and who writes them is covered in
[architecture.md](architecture.md#event-flow).

**Status:** the tables, the record-to-event conversion, the hash chain, the Postgres `RunStore`,
the per-run `.eval`, Parquet, and run summary exports, and the per-variant `.eval` assembly are
built (`swarmeval/db/`, `swarmeval/events/`). Items marked *(proposed)* are implementation details the specs
leave open. They are collected under [Not settled](#not-settled).

## Data model

Every event is a serialized Inspect `Event` at the pinned `inspect_ai` version. SwarmEval never
defines a second event structure. It extends Inspect in two ways only:

1. Custom fields go under `metadata["swarmeval"]`. Top-level fields are never added, because
   Inspect silently drops them on read.
2. A SwarmEval event with no Inspect counterpart is an `InfoEvent(source="swarmeval.<type>",
   data=...)`. `data` is validated by our own pydantic model for that type. Custom event types are
   never used, because one unknown type makes Inspect reject the whole sample.

| SwarmEval event | Inspect type |
|---|---|
| `llm.request` / `llm.response` | `ModelEvent`. The gateway's record goes in `metadata.swarmeval.gateway`: request hash, backend raw response, `reasoning_passback`, sampling actually sent, weight hash, latency, attempts |
| `tool.call` / `tool.result` | `ToolEvent`. The file changes (from schema version 6 with each path's `mtime_us`) and surviving processes sandboxd saw go in `metadata.swarmeval.exec`; a `web_request`'s exchange goes in `metadata.swarmeval.web` (schema version 3) |
| A command an extension ran through `ctx.sandbox` | `SandboxEvent` |
| `isolation_probe`: a command of the [isolation self-check](services/orchestrator.md#isolation-self-check) | `SandboxEvent`, with `metadata.swarmeval.probe` holding `step` (`plant`, `check`, `clean`) and, for `check`, `findings` (`probe`, `peer`, `outcome`, `detail`) beside `exec` (schema version 4). `sandbox_id` is the sandbox it ran in |
| Interrupted tool call | `InterruptEvent` |
| Recovery point | `CheckpointEvent` |
| Budget or limit hit | `SampleLimitEvent`: `turn`, `token`, or for `wall_clock` `working` |
| Score | `ScoreEvent` / `Score`, with `Score.metadata.swarmeval` holding `meaning`, `direction` (`1 = triggered`), and `event_ids` |
| `msg.send` / `msg.deliver`, `net.*`, `env.state` | `InfoEvent(source="swarmeval.<type>")` |
| Runtime records with no Inspect type: `lifecycle`, `intervention`, `extension`, `alert`, `final_diff`, `transcript_check` | `InfoEvent(source="swarmeval.<kind>")`, `data` is the record. A pause is a `lifecycle` event with `status: paused`, then one with `resumed` (schema version 6); a Monitor's hit is an `alert` |

One record is one event. The conversion is `swarmeval.events.convert.to_event`.

`metadata.swarmeval` on every event:

| Field | Meaning |
|---|---|
| `schema_version` | Version of the SwarmEval extension. The full log version is the pinned Inspect version plus this |
| `seq` | Run-wide sequence number, assigned by the run's worker |
| `parent_id` | The event that directly caused this one ([Causal parents](#causal-parents)). `null` only on the run's first event |
| `source` | `model-gateway`, `sandboxd`, or `orchestrator`; `net-gateway` with the network capability (later) |
| `agent_id`, `sandbox_id` | Attribution. Network events carry `sandbox_id`, because policy applies per sandbox |
| `workspace` | Organizational field, required from the first version |
| `extension` | Instance id of the extension that caused the event, if any |

Event ids are UUIDs the worker fixes when it builds an event, before it commits, so events
committed together can name each other as parents. The id becomes the Inspect event's `uuid`.

The hash chain is not in the payload, because the hash covers the payload. It lives in the
`prev_hash` / `hash` columns and is added to `metadata.swarmeval` on export.

Kind-specific fields also go under `metadata.swarmeval`. A `ModelEvent` carries `input` (the
`gen` / `len` reference, below), the offered `tools`, and `raw_tool_arguments`, the argument
text the model produced. A `ToolEvent` carries `raw_arguments`, `executed_arguments`,
`blocked_by`, and `exec`. Inspect keeps tool arguments as a parsed object. Arguments that are not
a JSON object, or hold a value JSON cannot carry exactly (NaN, a number that overflows a 64-bit
float, an integer beyond ±(2**53 − 1)),
are stored as `{}`, and the raw text is kept.

Run-level fields (`EvalSpec.metadata.swarmeval`) are `workspace`, the isolation level the run
actually got, `network_stealth`, and the recovery fidelity.

Concept mapping: a case is a Task. A variant is one `EvalLog` (`eval.model` + `eval.task_args`).
A run (variant × epoch) is one `EvalSample` with `sample.epoch`. A suite is an eval-set. An agent
is a `SpanBeginEvent` / `SpanEndEvent` pair with `type="agent"`. This mapping lets Inspect's epoch
reducers work on our epochs directly. Inspect's metrics run after the reducer, across samples, and
a variant has one sample, so the spread over epochs comes from metrics declared over unreduced
scores (see [the per-variant `.eval`](#the-per-variant-eval)).

### Causal parents

`parent_id` points to the event that directly caused this one, so any event can be traced back
along its parents to the run's first event, the run's root (M2 spec decision 1). The analysis
`trace` job prints such a chain ([analysis](services/analysis.md#capabilities)). An event has one
parent; when several things fed into it, the parent is the last one, and the rest is in the
`messages` table and the model call's `gen` / `len`.

| Event | `parent_id` |
|---|---|
| The run's first event | `null`. It is the self-check's first `isolation_probe` event, or `lifecycle` `started` in a run without a self-check |
| `isolation_probe` | The probe event before it, in commit order |
| `lifecycle` `started` | The event before it: the self-check's last probe |
| An agent's `ModelEvent` | The last event whose content was admitted into the agent's context before the request: a tool result (`ToolEvent`), a `msg.deliver`, an injection's `intervention`, a `before_turn` `Inject`, an `after_model_response` / `after_tool_result` rewrite (instead of the event it rewrote), a `compact_context` compaction, or `lifecycle` `started` for the generation it admitted. After a response with no tool calls and no new input, the agent's own previous `ModelEvent` |
| `ToolEvent` | The `ModelEvent` that made the call |
| `msg.send` | The `ModelEvent` that called `send_message`; for a message an extension posted, the `post` intervention |
| `msg.deliver` | The last `before_deliver` intervention on it for that recipient (a rewrite or a hold), whose content or timing it carries; with none, its `msg.send` *(proposed)* |
| `intervention` from a hook's return value | The hook's trigger (below): for a rewrite or a gate decision, the event it changed or decided on |
| `intervention` from an action (`stop`, `pause`, `inject`, `post`) | The action's `cause`: an event the extension names, such as an alert it just raised; without one, the hook's trigger |
| `alert` | The last of its `event_ids`; with none, the hook's trigger |
| `extension` (`ctx.emit`), an extension's `SandboxEvent`, an extension's own `ModelEvent` | The hook's trigger |
| `limit` | `lifecycle` `started` |
| `lifecycle` `finished`, `stopped`, `limit` | The event that ended the run: the `limit` event, or the intervention that stopped it (a `before_turn` `Stop`, or `ctx.actions.stop`). Otherwise, as for a cancel or a finish, `started` |
| `lifecycle` `failed` | `started`. After a failed self-check, the last probe |
| A fork's first event (schema version 7) | Its source's event at `fork_seq`, as `<source run>:<event id>` |
| What a fork carries over from its source (an agent's next model call, a carried delivery, a queued injection) | The source event it named there, as `<source run>:<event id>` |
| A fork's `intervention` (`hook: fork`) | The fork's `lifecycle` `started` |
| `lifecycle` `paused` | The `pause` intervention that asked for it (`ctx.actions.pause`), whose own parent is the Monitor's `alert` when a Monitor paused |
| `lifecycle` `resumed` | The `paused` event |
| `final_diff`, a scoring script's `SandboxEvent` | The run's last `lifecycle` event |
| `score`, `transcript_check` | The last event in its `event_ids`; with none, the run's last `lifecycle` event |

A hook's trigger is the event that caused the call, given to the hook as `ctx.trigger_id`:

| Hook | Trigger |
|---|---|
| `on_event` | The committed event |
| `after_model_response`, `before_tool_call`, a worker tool | The `ModelEvent` |
| `after_tool_result` | The `ToolEvent` |
| `before_deliver` | The `msg.send` |
| `before_turn`, `compact_context`, `before_model_request`, `after_turn` | The agent's last admitted event, the parent its next model call would get |
| `on_run_start`, `on_resume`, `on_run_end` | `lifecycle` `started` |

A `before_deliver` intervention's `after` is the verdict with the recipient:
`{"recipient", "kind": "deliver", "content"}`, `{"recipient", "kind": "drop", "reason"}`, or
`{"recipient", "kind": "delay", "turns"}`; `before_sha256` is the hash of the content it was
given. A `post` intervention's `after` is `{"channel", "sender", "content"}`.

Events written before schema version 5 have a parent only where the first rows of the first table
say (`ToolEvent`, `msg.deliver`, and interventions on a model or tool event). The trace of such an
event stops at the first event without one.

### Versioning

A change to any `swarmeval` extension model bumps `schema_version`. The reader keeps loading every
older version (see AGENTS.md "Event schema"). Upgrading `inspect_ai` is its own change: bump the
pin and run the read-back tests. Inspect reads older logs, but not newer ones, so a log produced
after an upgrade may require readers to upgrade too. `EvalSpec.packages` records the version that
produced it.

## Tables

All in the `runs` schema of the one Postgres instance, owned by the orchestrator, keyed by
`run_id`. Migrations are Alembic and only add columns and indexes. `payload` and the hash columns
of `events` are never rewritten.

| Table | Key | Columns | Written |
|---|---|---|---|
| `events` | `(run_id, seq)` | `event_id`, `ts`, `type`, `source`, `agent_id`, `sandbox_id`, `parent_id`, `prev_hash`, `hash`, `payload jsonb` | Append only |
| `messages` | `(run_id, agent_id, gen, idx)` | One context message (the runtime's `ChatMessage` as JSON), and `seq` | Append only |
| `agent_state` | `id`, indexed on `(run_id, agent_id, seq, id)` | `gen`, `len`, `turn`, `status`, `tokens_used` | Append only, one row per state change |
| `extension_state` | `id`, indexed on `(run_id, instance_id, seq, id)` | An extension instance's state, `jsonb`, and `rng_uses`, how many of its calls have drawn from `ctx.rng` (migration 0007; `0` for older rows) | Append only |
| `deliveries` | `(run_id, msg_seq, recipient)` | `status`, `delivered_seq`, `due_turn` | Updated. The store writes it from `msg.send` and `msg.deliver` events and from the `before_deliver` verdicts the loop passes, each in their transaction |
| `checkpoints` | `(run_id, turn)`, indexed on `(run_id, seq)` | `seq`, the last event before the turn, and `state jsonb`: the loop's state at the start of each run-wide turn once the observers caught up ([below](#checkpoints)) (migration 0008) | Append only |
| `canaries` | `run_id` | The run's file and sandbox canary tokens, written before its sandboxes exist, so a fork plants the same (migration 0008) | Once |
| `sandboxes` | `(run_id, sandbox_id)` | Container id, recovery fidelity | Updated. Not built yet |

A `deliveries` row starts `pending` when its send commits. `before_deliver` may make it `dropped`,
which is final: no delivery, recovery, or fork ever delivers it, and the store refuses a
`msg.deliver` for it. Or `delayed`, with `due_turn`, the recipient's own turn at whose start it
is delivered. A `pending` or `delayed` row becomes `delivered` with the `msg.deliver`'s `seq`
(`due_turn` stays). A row still `delayed` or `pending` when the run ends was never delivered.
Only a `pending` row takes a verdict (migration 0007 added `dropped`, `delayed`, `due_turn`).

`seq` on a `messages`, `agent_state`, or `extension_state` row is the run's last event `seq` when
the row was committed. Many transactions commit no event, so several rows can share a `seq`; the
identity column orders them. `event_id` is the Inspect event's `uuid`. `type` is the Inspect event
type, or an `InfoEvent`'s `swarmeval.<kind>` source. `working_start` in the payload is seconds
since the run's first event.

`control.runs` holds `run_id`, `workspace`, `status`, `owner_id`, `lease_until`, `owner_epoch`,
`created_at`, `started_at`, `finished_at`, `isolation`, `fidelity` (a fork's, migration 0008),
`error`, and `cancelled_by` and `resumed_by`, the actors edge named on the last cancel and resume
(migration 0009). Every `runs` table references it. `control.cases` and `control.case_revisions`
are the [case library](services/orchestrator.md#case-library) (migration 0010).
`control.run_specs` holds each run's `submission_id`, `case_id`, `case_sha256`,
`case_revision_id` (the library revision whose bundle that hash is), `overrides`, `models`
(model slot → the model the run uses for it, migration 0013; null for runs queued before
[model slots](case-format.md#model-slots)), `variant`, `task_args` (with each model as
`model.<slot>`, schema version 9), `epoch`, `epochs`, `replaces`, the interrupted run a
[rerun](services/orchestrator.md#reruns) stands in for, and `suite`, the
[suite](services/orchestrator.md#suites) label of the submission, and for a [fork](services/orchestrator.md#forks) `forked_from`,
`fork_seq`, and `fork_edits` (migration 0008), and `submitted_by`, the actor of the submission or
fork that queued the run, which a rerun keeps (migration 0009).

An agent's context is the `(gen, len)` of its latest `agent_state` row, followed by the first
`len` rows of `messages` for that `gen`. Compaction or truncation starts a new `gen`, and old
generations are kept. Recovery (M3) reads the latest row.

### Checkpoints

A fork reads its source's state from `checkpoints`, not from the other tables cut at a `seq`:
rows committed without an event carry the previous event's `seq`, so a `seq` cutoff cannot tell
the rows before a turn from the first ones of it. A checkpoint (`swarmeval.runtime.records.
Checkpoint`) holds the run-wide `turn` count, `round`, the agents left in the current
`round_robin` round, `tokens_used`, per agent its `gen`, `len`, own `turn`, `finished`, and
`last_input` (the parent of its next model call), every extension instance's state and
`rng_uses`, the routed mail not yet delivered (send, content after `before_deliver`, `due_turn`,
parent), the injections and posts extensions queued, and how many `ctx.spawn` tasks were still
running (a fork refuses a checkpoint with any). Messages are not copied: `gen` and `len`
locate them in `messages`, which is append only.

Derived results, such as later rule matches and judge verdicts, go to separate tables and never
touch `events` (see [analysis](services/analysis.md#outputs)).

`ModelEvent.input` is stored as a reference: `agent_id`, plus `gen` and `len` in
`metadata.swarmeval.input`, taken from the request the loop built. It is expanded when `.eval` is
exported. `metadata.swarmeval.gateway.request_sha256` is the hash of the request body as the
gateway received it; the worker checks it against the body it sent. A model call not
built from an agent's context (an extension's own call) has `gen: null`, and its input is not
stored yet *(open)*. Writing the full context on every call would make
storage grow with the square of the step count. If the gateway's request hash disagrees with the
expanded input, that is a spoofing signal; the [transcript check](#transcript-check) looks for it
at run end.

## Transcript check

At run end, after the final-state scorers and before export, the worker compares what each agent
saw with what the sources recorded (v1 spec §6; agent loop spec decision 2). It reads the run's
rows, not its memory (`swarmeval.events.transcript.load_transcript`), compares them
(`swarmeval.worker.transcript.check_transcript`), and commits the outcome as the run's last
event, `InfoEvent(source="swarmeval.transcript_check")`, so both exports carry it. Compared:

| Check | What must hold | Mismatch names |
|---|---|---|
| `context` | Walking each agent's generations in `idx` order: generation 0 starts with the case's system prompt and task; a later generation starts with the `after` of a `compact_context` intervention. After that, an assistant message at index *i* of generation *g* is the response of the agent's model call built from `(g, i)`; the tool messages after it are the results of that call's `ToolEvent`s (`parent_id`), in `seq` order, with the call's id; a user message is a `msg.deliver` to this agent (as the bus words it), an injection into it (`ctx.actions.inject`), or a `before_turn` `Inject`. Where an `after_model_response` / `after_tool_result` intervention rewrote the event, the last rewrite's `after` is what must appear instead | The model or tool event the message was compared with; none when no event could explain it. A generation whose start nothing explains is reported once and not walked further |
| `request` | Each agent model call's request, rebuilt from the stored context (`gen`, `len`), the tools it offered, and its sampling options, hashes (sha256 of the wire body the worker sends) to the gateway's `request_sha256` | The model event |
| `response` | Each model call's response equals the gateway's `upstream_response_json` normalized again the way model-gateway normalizes it | The model event |
| `send` | An agent's `msg.send` has the channel and content its `send_message` call ran with: the `ToolEvent` with the send's `call_id` under the same model event, by its executed arguments. A `msg.send` with no agent has a `post` intervention as parent with the same channel, sender, and content | The `msg.send` |
| `delivery` | A `msg.deliver` names a recorded send, has its channel and sender, and carries its content, or the content of the last `before_deliver` rewrite (`action` `deliver`) of it for that recipient; and no `drop` for that recipient precedes it | The `msg.deliver` |

`data` holds `consistent`, the counts compared (`messages`, `requests`, `responses`, and
`deliveries`, from schema version 5), the intervention events that explained a difference or an
absence (`interventions`: rewrites, compactions, and injections a context message matched;
`before_deliver` verdicts a delivery matched; and drops and holds of messages a recipient never
got), each mismatch (`check`,
`agent_id`, `gen`, `idx`, `event_id`, `detail`), and `event_ids`, every event a mismatch names.
A difference an intervention explains is an intervention; any other is a mismatch, the spoofing
signal. A mismatch does not fail the run; the worker logs a warning.

Each source explains one message. Not covered: an extension's own model calls have no stored
input, so only their responses are checked; a `before_turn` `Inject` names no agent, so it can
explain an equal message in any agent's context; and content that merely looks like a tool call
or result inside a recorded result is not a mismatch, since the event recorded it so. Rule scans
and the judge look at content.

Added in schema version 4 (with `isolation_probe`); older runs simply lack it. The `send` and
`delivery` checks and `deliveries` are from schema version 5.

A fork's transcript also holds its sources' events up to the fork point (`lineage`), which explain
the contexts it copied at their generation numbers; their own requests, responses, sends, and
deliveries were checked in the source and are not checked again. A fork's `edit_context`
intervention starts a generation the way a compaction does, and its `deliver` intervention
counts as the last rewrite of that message for that recipient. Its `replace_model` intervention
(schema version 9) changes no context: `after` names the model slot and the models `before` and
after (`model`), and the agents of that slot call the new model from there on.

## Hash chain

Each run has one chain, extended by its only writer:

```text
hash[seq] = sha256( prev_hash || uint64_be(seq) || JCS(payload) )
prev_hash of the first event = sha256("swarmeval:" || run_id)
```

A fork's chain links into its source's: its first event has `seq` = `fork_seq` + 1 and, as
`prev_hash`, the source's hash at `fork_seq` (`ChainStart`). `working_start` keeps counting from
the source's first event. Verifying a fork's rows needs that one hash of the source.

`JCS` is the RFC 8785 JSON Canonicalization Scheme. `jsonb` does not keep the bytes it was given,
so the hash has to be over a form that can be recomputed from the stored value. `payload` is the
event serialized without `None` fields. Anyone holding the rows, or the exported Parquet, can
verify the chain without our code; `swarmeval.events.verify` does it for us *(proposed)*.

A payload JSON cannot represent exactly (NaN, infinities, integers beyond ±(2**53 − 1)) cannot be
hashed, and the commit fails with the event's kind and agent. Strings must not contain NUL,
which `jsonb` rejects. The model-gateway and sandboxd clients are the boundaries that must
guarantee both.

## Commit paths

Every event is committed by the run's worker, in a transaction that first checks `owner_epoch`
([orchestrator](services/orchestrator.md#leases-fencing-and-takeover)). The check is built: a
store holding a stale epoch raises `FencedError` and writes nothing. The worker acks the source
only after the commit.

| Events | Transaction |
|---|---|
| `ModelEvent` | Alone with the `messages` / `agent_state` rows it produces. Acked, then the gateway returns the response |
| `ToolEvent` with its `fs.*` / `proc.*` | One transaction with the tool result. A `web_request` (M1) is a `ToolEvent` like any other |
| `net.*` (network capability, later) | Group commit about every 100 ms, then one ack per batch *(open, runtime spec Q2)* |
| Message Bus, lifecycle, monitor | With the state change they describe |

Each transaction ends with `NOTIFY swarmeval_events, '<run_id>'`. Postgres delivers the
notification only if the transaction commits. The control plane uses it as a wakeup for live
streams.

## Large objects

pcaps, file contents, and oversized tool output go to object storage under
`blobs/sha256/<hex>`, and the event stores only the hash. The worker uploads a blob before it
commits the event that references it, so a committed hash always resolves. Because keys are
content addresses, a retried upload is harmless *(proposed)*.

Built for sandboxd's blobs: `swarmeval.sandbox.S3BlobStore` writes them, and the sandboxd client
uploads them before `Exec` or `FinalDiff` returns. In a `ToolEvent`'s `metadata.swarmeval.exec`,
`stdout_truncated` / `stderr_truncated` name the blob holding the full output, and a change with
`content_stored` has its new content under `after_sha256`.

## Export

At run end the worker writes to the export bucket, which is created with object lock:

| Object | Content | From |
|---|---|---|
| `runs/<run_id>/sample.eval` | Standard Inspect log, readable by `inspect view`. `ModelEvent.input` expanded | M0 |
| `runs/<run_id>/events.parquet` | One row per event: the indexed columns, `prev_hash` / `hash` as hex, and `payload` as JSON text. Written after the `.eval`, so only for runs that verified and ended `done` or `cancelled` | Built |
| `summaries/<run_id>.parquet` | One row per run: submission and its suite label, case and its hash, variant and `task_args` (JSON, sorted keys), epoch and the epochs requested per variant, `replaces` and `replaced_by` (an interrupted run and its [rerun](services/orchestrator.md#reruns)), status and error, isolation, times, and each scorer's last score. Written for every run that reaches a final status, copying the status `control.runs` holds: by the worker once it has finished a run (a cancel that lands meanwhile wins), by the Control API when it cancels a queued run, by a worker marking its old runs `interrupted` at start, and by a worker that took over a run whose lease ran out. If the worker cannot write one, a `done` run becomes `failed` and an `interrupted` (its rerun stays queued), `failed`, or `cancelled` run keeps its status; either way its error adds why. At start, the worker does not start. Reports read only these. A column added later reads as null in older summaries | Built |

Code: `swarmeval.events.export_events`, `export_summary`, and the schemas `EVENTS_SCHEMA` and
`SUMMARY_SCHEMA`. Object paths are *(proposed)*. The per-variant `.eval` is not in the bucket: the
analysis `eval-set` job assembles it from these per-run logs into a local directory
([below](#the-per-variant-eval)).

How a run becomes a `.eval` (`swarmeval.events.export_run`):

1. Read the run's `events` and `messages` rows, and verify the chain. Rows that do not verify
   raise `ChainError`, and nothing is written.
2. Rebuild each event from its payload. Add `prev_hash` / `hash` (hex) to `metadata.swarmeval`,
   and expand `ModelEvent.input` from `messages`.
3. Wrap each agent's events in a `SpanBeginEvent` / `SpanEndEvent` with `type="agent"` and id
   `agent:<agent_id>`, and set their `span_id`. Events with no agent stay at the top level.
4. Build one `EvalSample`: `id` is the case id, `uuid` the run id, `input` the case task. There is
   no target and no `messages` list, since a swarm has one conversation per agent; the
   transcript is in `events`. A `SampleLimitEvent` becomes `sample.limit`, model usage is
   summed per model, and each scorer's last `ScoreEvent` becomes `sample.scores[<scorer>]` and
   an `EvalScore` in `results` with a `mean` metric.
5. Build the `EvalLog` from a `RunHeader` the worker supplies (case id, variant index and axis
   values, epoch, agent models). `eval.model` is the first agent's model; every agent's model is
   in `eval.metadata.swarmeval.models`; `eval.metadata.swarmeval.deterministic` is `false` for
   an `async` run, whose event order is recorded but not reproducible. A fork's sample metadata
   names `forked_from` and `fork_seq`. A run whose last lifecycle event is `failed` has status
   `error`.
6. Write the log with `inspect_ai` to a scratch file and upload it with `pyarrow.fs`.

### The per-variant `.eval`

Assembled by the analysis `eval-set` job ([analysis](services/analysis.md#capabilities)), not the
control plane: it already reads every epoch, and the worker's per-run logs stay the only objects
written to the bucket. `swarmeval.events.variant_log` takes the per-run logs of one submission's
case revision (`case_sha256`) and variant:

1. Only runs that ended `done` become samples, one per epoch, each the run's own sample
   (`sample.epoch`, `sample.uuid` = run id). Runs that ended otherwise are listed in
   `eval.metadata.swarmeval.left_out` with their epoch and status, as the report lists them
   (runtime spec Q6). Two runs of one epoch, or runs whose task, `task_args`, models, or
   workspace differ, are refused.
2. The header is the first run's, with `eval.run_id` = submission id,
   `eval.config.epochs_reducer` = the job's reducers (default `mean`, runtime spec decision 11),
   `eval.metadata.swarmeval` adding `submission_id`, `case_sha256`, and `runs` (epoch → run id),
   and one `eval.scorers` entry per scorer declaring its metrics.
3. `results` and `reductions` are computed by Inspect (`inspect_ai.log.recompute_metrics`) from
   that header. Per scorer: one score per reducer, with Inspect's `mean` of the reduced value (the
   trigger rate under `mean`); and one score with no reducer holding `epoch_stderr` and
   `epoch_ci_wilson` (`lower`, `upper`), Inspect's `stderr` and `ci_wilson` declared over
   unreduced scores, so each epoch is one observation, as in the report. Those two metrics are
   registered by `swarmeval.events` as `swarmeval/epoch_stderr` and
   `swarmeval/epoch_ci_wilson`; viewing the log needs nothing, recomputing it needs `swarmeval`
   importable.

A reducer is chosen per job, not per scorer: Inspect keeps one `epochs_reducer` list per log, and
`case.yaml` has no field for it.

The rows in `runs` are the evidence original. Exports are derived from them, and audit, replay,
and spoofing checks use the database. Cleanup of old runs, and whether Parquet then becomes the
archive original, is *(open, runtime spec Q3)*.

## Not settled

1. JCS over `payload` as the hashed form, and the genesis value.
2. Blob key layout, and upload before commit.
3. Export object paths.
4. ~~Which service assembles the per-variant `.eval` in M1: the control plane, when a variant's
   last epoch finishes, or the analysis batch job, which already reads all epochs.~~ Settled
   (2026-10-02): the analysis `eval-set` job, writing to a local directory
   ([The per-variant `.eval`](#the-per-variant-eval)).
5. File changes and surviving processes ride in the `ToolEvent`'s `metadata.swarmeval.exec`,
   one record per event. The runtime spec (decision 3) describes them as their own `fs.*` /
   `proc.*` events. Event ids are now fixed before commit, so a child could name its `ToolEvent`
   as parent within one transaction; the split itself is not done.
6. *(proposed)* The parents the M2 spec's table (decision 1) leaves open: the self-check's probes
   form one chain from the run's first event, run-level events descend from `lifecycle`
   `started` or the run's last `lifecycle` event, and a hook's trigger for the hooks that concern
   no single event is as in [Causal parents](#causal-parents).
