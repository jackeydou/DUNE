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
| `tool.call` / `tool.result` | `ToolEvent`. The file changes and surviving processes sandboxd saw go in `metadata.swarmeval.exec`; a `web_request`'s exchange goes in `metadata.swarmeval.web` (schema version 3) |
| A command an extension ran through `ctx.sandbox` | `SandboxEvent` |
| `isolation_probe`: a command of the [isolation self-check](services/orchestrator.md#isolation-self-check) | `SandboxEvent`, with `metadata.swarmeval.probe` holding `step` (`plant`, `check`, `clean`) and, for `check`, `findings` (`probe`, `peer`, `outcome`, `detail`) beside `exec` (schema version 4). `sandbox_id` is the sandbox it ran in |
| Interrupted tool call | `InterruptEvent` |
| Recovery point, pause | `CheckpointEvent` |
| Budget or limit hit | `SampleLimitEvent` |
| Score | `ScoreEvent` / `Score`, with `Score.metadata.swarmeval` holding `meaning`, `direction` (`1 = triggered`), and `event_ids` |
| `msg.send` / `msg.deliver`, `net.*`, `env.state`, `monitor.*`, `run.lifecycle` | `InfoEvent(source="swarmeval.<type>")` |
| Runtime records with no Inspect type: `lifecycle`, `intervention`, `extension`, `alert`, `final_diff`, `transcript_check` | `InfoEvent(source="swarmeval.<kind>")`, `data` is the record |

One record is one event. The conversion is `swarmeval.events.convert.to_event`.

`metadata.swarmeval` on every event:

| Field | Meaning |
|---|---|
| `schema_version` | Version of the SwarmEval extension. The full log version is the pinned Inspect version plus this |
| `seq` | Run-wide sequence number, assigned by the run's worker |
| `parent_id` | Causal parent event |
| `source` | `model-gateway`, `sandboxd`, or `orchestrator`; `net-gateway` with the network capability (later) |
| `agent_id`, `sandbox_id` | Attribution. Network events carry `sandbox_id`, because policy applies per sandbox |
| `workspace` | Organizational field, required from the first version |
| `extension` | Instance id of the extension that caused the event, if any |

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
| `extension_state` | `id`, indexed on `(run_id, instance_id, seq, id)` | An extension instance's state, `jsonb` | Append only |
| `deliveries` | `(run_id, msg_seq, recipient)` | `status` (`pending`, `delivered`), `delivered_seq` | Updated. The store writes it from `msg.send` and `msg.deliver` events, in their transaction |
| `sandboxes` | `(run_id, sandbox_id)` | Container id, recovery fidelity | Updated. Not built yet |

`seq` on a `messages`, `agent_state`, or `extension_state` row is the run's last event `seq` when
the row was committed. Many transactions commit no event, so several rows can share a `seq`; the
identity column orders them. `event_id` is the Inspect event's `uuid`. `type` is the Inspect event
type, or an `InfoEvent`'s `swarmeval.<kind>` source. `working_start` in the payload is seconds
since the run's first event.

`control.runs` holds `run_id`, `workspace`, `status`, `owner_id`, `lease_until`, `owner_epoch`,
`created_at`, `started_at`, `finished_at`, `isolation`, and `error`. Every `runs` table references
it. `control.run_specs` holds each run's `submission_id`, `case_id`, `case_sha256`, `overrides`,
`variant`, `task_args`, `epoch`, `epochs`, `replaces`, the interrupted run a
[rerun](services/orchestrator.md#reruns) stands in for, and `suite`, the
[suite](services/orchestrator.md#suites) label of the submission.

An agent's context at step *k* is the `(gen, len)` from its last `agent_state` row with
`seq ≤ k`, followed by the first `len` rows of `messages` for that `gen`. Compaction or truncation
starts a new `gen`, and old generations are kept. Recovery reads the latest row, and fork reads
the row at step *k*. It is the same query, and its cost does not depend on run length.

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

`data` holds `consistent`, the counts compared (`messages`, `requests`, `responses`), the
intervention events that explained a difference (`interventions`), each mismatch (`check`,
`agent_id`, `gen`, `idx`, `event_id`, `detail`), and `event_ids`, every event a mismatch names.
A difference an intervention explains is an intervention; any other is a mismatch, the spoofing
signal. A mismatch does not fail the run; the worker logs a warning.

Each source explains one message. Not covered: an extension's own model calls have no stored
input, so only their responses are checked; a `before_turn` `Inject` names no agent, so it can
explain an equal message in any agent's context; and content that merely looks like a tool call
or result inside a recorded result is not a mismatch, since the event recorded it so. Rule scans
and the judge look at content.

Added in schema version 4 (with `isolation_probe`); older runs simply lack it.

## Hash chain

Each run has one chain, extended by its only writer:

```text
hash[seq] = sha256( prev_hash || uint64_be(seq) || JCS(payload) )
prev_hash of the first event = sha256("swarmeval:" || run_id)
```

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
| `summaries/<run_id>.parquet` | One row per run: submission and its suite label, case and its hash, variant and `task_args` (JSON, sorted keys), epoch and the epochs requested per variant, `replaces` and `replaced_by` (an interrupted run and its [rerun](services/orchestrator.md#reruns)), status and error, isolation, times, and each scorer's last score. Written for every run that reaches a final status, copying the status `control.runs` holds: by the worker once it has finished a run (a cancel that lands meanwhile wins), by the Control API when it cancels a queued run, and by a worker marking its old runs `interrupted` at start. If the worker cannot write one, the run is `failed` and says why; at start, the worker does not start. Reports read only these. A column added later reads as null in older summaries | Built |

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
   in `eval.metadata.swarmeval.models`. A run whose last lifecycle event is `failed` has status
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
   `proc.*` events. Splitting them out needs event ids assigned before commit, so a child can
   name its `ToolEvent` as parent within one transaction.
