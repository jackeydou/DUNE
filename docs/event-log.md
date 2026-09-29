# Event log

The event log is the evidence record of a run: what was stored, in what format, how it is chained,
and how it leaves the database. Its code is `swarmeval/events/`, inside the
[orchestrator](services/orchestrator.md). That package is the only place that imports
`inspect_ai`. Where events come from and who writes them is covered in
[architecture.md](architecture.md#event-flow).

**Status:** not built. The table layout, hash chain, and `.eval` export arrive in M0. Parquet and
the run summary table arrive in M2. Items marked *(proposed)* are implementation details the specs
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
| `llm.request` / `llm.response` | `ModelEvent`. Raw output text, `reasoning_passback`, and the weight hash go in metadata |
| `tool.call` / `tool.result` | `ToolEvent` |
| Sandbox execution and file I/O at the tool level | `SandboxEvent` |
| Interrupted tool call | `InterruptEvent` |
| Recovery point, pause | `CheckpointEvent` |
| Budget or limit hit | `SampleLimitEvent` |
| Score | `ScoreEvent` / `Score` |
| `msg.send` / `msg.deliver`, `net.*`, `fs.*` / `proc.*`, `env.state`, `monitor.*`, `run.lifecycle` | `InfoEvent(source="swarmeval.<type>")` |

`metadata.swarmeval` on every event:

| Field | Meaning |
|---|---|
| `schema_version` | Version of the SwarmEval extension. The full log version is the pinned Inspect version plus this |
| `seq` | Run-wide sequence number, assigned by the run's worker |
| `parent_id` | Causal parent event |
| `source` | `model-gateway`, `net-gateway`, `sandboxd`, or `orchestrator` |
| `agent_id`, `sandbox_id` | Attribution. Network events carry `sandbox_id`, because policy applies per sandbox |
| `workspace` | Organizational field, required from the first version |
| `prev_hash`, `hash` | Hash chain, below |

Run-level fields (`EvalSpec.metadata.swarmeval`) are `workspace`, the isolation level the run
actually got, `network_stealth`, and the recovery fidelity.

Concept mapping: a case is a Task. A variant is one `EvalLog` (`eval.model` + `eval.task_args`).
A run (variant × epoch) is one `EvalSample` with `sample.epoch`. A suite is an eval-set. An agent
is a `SpanBeginEvent` / `SpanEndEvent` pair with `type="agent"`. This mapping lets Inspect's epoch
reducers and `stderr` work on our epochs directly.

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
| `messages` | `(run_id, agent_id, gen, idx)` | One context message, and the `seq` that produced it | Append only |
| `agent_state` | `(run_id, agent_id, seq)` | `gen`, `len`, turn, status, budget used | Append only, one row per step |
| `deliveries` | `(run_id, msg_seq, recipient)` | Status, `seq` of the delivery | Updated |
| `sandboxes` | `(run_id, sandbox_id)` | Container id, recovery fidelity | Updated |

An agent's context at step *k* is the `(gen, len)` from its last `agent_state` row with
`seq ≤ k`, followed by the first `len` rows of `messages` for that `gen`. Compaction or truncation
starts a new `gen`, and old generations are kept. Recovery reads the latest row, and fork reads
the row at step *k*. It is the same query, and its cost does not depend on run length.

Derived results, such as later rule matches and judge verdicts, go to separate tables and never
touch `events` (see [analysis](services/analysis.md#outputs)).

`ModelEvent.input` is stored as a reference: `(agent_id, gen, len)` plus a hash of the request
body. It is expanded when `.eval` is exported. Writing the full context on every call would make
storage grow with the square of the step count. If the gateway's request hash disagrees with the
expanded input, that is a spoofing signal *(open, runtime spec Q1)*.

## Hash chain

Each run has one chain, extended by its only writer:

```text
hash[seq] = sha256( prev_hash || uint64_be(seq) || JCS(payload) )
prev_hash of the first event = sha256("swarmeval:" || run_id)
```

`JCS` is the RFC 8785 JSON Canonicalization Scheme. `jsonb` does not keep the bytes it was given,
so the hash has to be over a form that can be recomputed from the stored value. Anyone holding
the rows, or the exported Parquet, can verify the chain without our code *(proposed)*.

## Commit paths

Every event is committed by the run's worker, in a transaction that first checks `owner_epoch`
([orchestrator](services/orchestrator.md#leases-fencing-and-takeover)). The worker acks the
source only after the commit.

| Events | Transaction |
|---|---|
| `ModelEvent` | Alone with the `messages` / `agent_state` rows it produces. Acked, then the gateway returns the response |
| `ToolEvent` with its `fs.*` / `proc.*` | One transaction with the tool result |
| `net.*` | Group commit about every 100 ms, then one ack per batch *(open, runtime spec Q2)* |
| Message Bus, lifecycle, monitor | With the state change they describe |

Each transaction ends with `NOTIFY swarmeval_events, '<run_id>'`. Postgres delivers the
notification only if the transaction commits. The control plane uses it as a wakeup for live
streams.

## Large objects

pcaps, file contents, and oversized tool output go to object storage under
`blobs/sha256/<hex>`, and the event stores only the hash. The worker uploads a blob before it
commits the event that references it, so a committed hash always resolves. Because keys are
content addresses, a retried upload is harmless *(proposed)*.

## Export

At run end the worker writes to the export bucket, which is created with object lock:

| Object | Content | From |
|---|---|---|
| `runs/<run_id>/sample.eval` | Standard Inspect log, readable by `inspect view`. `ModelEvent.input` expanded | M0 |
| `runs/<run_id>/events.parquet` | One row per event: the indexed columns, the hash columns, and `payload` as JSON text | M2 |
| `summaries/<run_id>.parquet` | One row per run, like Inspect's log header. List queries read only these | M2 |

Object paths are *(proposed)*. From M2, one `.eval` per variant is assembled once all its epochs
finish. Which service assembles it is [not settled](#not-settled).

The rows in `runs` are the evidence original. Exports are derived from them, and audit, replay,
and spoofing checks use the database. Cleanup of old runs, and whether Parquet then becomes the
archive original, is *(open, runtime spec Q3)*.

## Not settled

1. JCS over `payload` as the hashed form, and the genesis value.
2. Blob key layout, and upload before commit.
3. Export object paths.
4. Which service assembles the per-variant `.eval` in M2: the control plane, when a variant's last
   epoch finishes, or the analysis batch job, which already reads all epochs.
