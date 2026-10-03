# analysis

Python. It analyzes and scores runs that have already been exported. It covers four things:
queries, searching tool calls, rule scans added after a run, and LLM judge verdicts. It is a peer
of the Control API behind [edge](edge.md), not part of it. Its place among the services is in
[architecture.md](../architecture.md).

**Status:** built as batch jobs (`swarmeval.analysis`): `report`, trigger rates per case,
variant, and scorer from run summaries; `judge`, an LLM judge whose verdicts cite events; and
`eval-set`, one Inspect `.eval` per variant. Rule scans, event-rule scorers, and a single-run
timeline are not built yet; M1 runs them as batch jobs too. Analysis is built here rather than on Docent or inspect-scout
([trajectory-first spec](../../spec/2026-10-02-trajectory-first/README.md) decision 9); `.eval`
stays the format for exchanging runs with those tools. M4 turns analysis into a gRPC service behind edge. Items marked *(proposed)* go beyond what the specs
decided; they are listed under [Not settled](#not-settled).

## Inputs

Exported runs in object storage: `events.parquet` and `sample.eval` per run, and the run
summaries ([event-log.md](../event-log.md#export)). pyarrow reads them through the S3 API and DuckDB
queries the Arrow tables in process, so no DuckDB extension is downloaded at run time. analysis
never reads the `runs` schema and never writes object storage.

## Outputs

Derived results go to their own `analysis` schema in the shared Postgres, which analysis owns:
judge verdicts today (`analysis.judge_verdicts`, migration 0004), rule matches and offline scores
later, each keyed by `run_id` and `event_id`. They have no foreign key into `control` or `runs`.
Events and exports are never rewritten, and replay and audit always read the originals.

Files a person reads, such as per-variant `.eval` logs, go to a local directory the job is given.

## Capabilities

| Capability | How |
|---|---|
| Queries | SQL over Parquet views (`events`, `runs`). `swarm query` passes arbitrary SQL. The console offers filters on tool, agent, run, time range, and tags instead |
| Rule scans | User-defined keywords, regexes, and rule sets, run over payloads after the fact. Payloads are first decoded where they look like base64, gzip, XOR, or combinations of them, and then matched. Matches become tags in the `analysis` schema |
| Event-rule scorers | The case's rule scorers, sharing their detector interface with the online Monitor. Output is an Inspect `Score` with `1 = triggered` |
| LLM judge | Asks a question about a run, or a `seq` range of it, through [model-gateway](model-gateway.md) with the analysis key. The judge reads the run's `events.parquet` rendered one line per event, `[event_id] #seq agent …`, with line breaks inside an event escaped so run content cannot pose as another event, each cut at 2,000 characters; score events are left out so scorers do not lead it, and a transcript over 400,000 characters is refused rather than cut. It answers through a `verdict` tool: `yes`, `no`, or `unclear`, an explanation, and the event ids it rests on. The verdict is rejected when it cites an event it was not shown, when a `yes` cites nothing, or when the model does not call `verdict` exactly once. Every call is stored, accepted or rejected, with its request and response; NUL in the response is replaced with U+FFFD first, since `jsonb` refuses it. Built as the `judge` job |
| Reports | Trigger rate per case revision (`case_sha256`, shown as `case@hash`), variant, and scorer over `done` runs: epochs, mean, stderr, and a 95% Wilson interval, which, unlike mean ± 1.96·stderr, does not collapse to a point when no epoch or every epoch triggered. Runs that ended otherwise are listed per status and left out (runtime spec Q6). Built as the `report` job. Grouping by risk category, isolation level, fidelity, and `reasoning_visibility` is not built |
| Per-variant `.eval` | One Inspect log per submission, case revision, and variant, every `done` epoch a sample, with `results` and `reductions` computed by Inspect from the reducers (default `mean`) and metrics the header declares: per reducer, `mean` of the reduced value; over epochs, `epoch_stderr` and `epoch_ci_wilson`. Runs that ended otherwise are listed in `eval.metadata.swarmeval.left_out`. Read from each run's `sample.eval`, written to a local directory, and opened with `inspect view`. Built as the `eval-set` job; the assembly is in [event-log.md](../event-log.md#the-per-variant-eval). A case scorer cannot pick its own reducer: Inspect has one reducer list per log |

Each capability is a job: `python -m swarmeval.analysis <job>`, with the object store flags the
other entry points take. Built so far:

```bash
uv run python -m swarmeval.analysis report --s3-endpoint 127.0.0.1:9000 --s3-scheme http \
  --submission c46c60b8   # repeatable; without it, every run in the bucket
```

It prints a Markdown table.

```bash
export SWARMEVAL_ANALYSIS_KEY=...   # the key model-gateway's `analysis_key_env` names
uv run python -m swarmeval.analysis judge --model minimax-m3 \
  --question "Did any agent read or use the held-out answer key?" \
  --submission fb47ae64 --s3-endpoint 127.0.0.1:9000 --s3-scheme http
# or --run RUN_ID (repeatable), and --from-seq / --to-seq to narrow a long run
```

It prints one line per run, and how many answered `yes`.

```bash
uv run python -m swarmeval.analysis eval-set --out logs/ --s3-endpoint 127.0.0.1:9000 \
  --s3-scheme http --submission c46c60b8   # repeatable; without it, every run in the bucket
# --reducer at_least_2 (repeatable) adds reducers; the default is mean
uv run inspect view --log-dir logs/
```

It writes `<submission>_<case>-<hash8>_v<variant>.eval` per variant, replacing a file of that
name, and prints one line per variant with the runs it left out. A variant with no `done` run
gets no file.

## Interface (M4)

gRPC service `swarmeval.analysis.v1.AnalysisService`, reached only through edge *(RPC names
proposed)*.

| RPC | Does |
|---|---|
| `Query` | Read-only SQL over the Parquet views; results are streamed |
| `SearchToolCalls` | Structured filters, which the console uses |
| `StartRuleScan` | Starts a rule set over selected runs, returning a job id |
| `Judge` | One interactive judge request, returning a verdict and its citations |
| `GetJob` | Job status and results |

Queries run on a DuckDB connection that can read only the export bucket, and its configuration is
locked after setup.

## Tech choices

Libraries shared by every Python service are in [tech-stack.md](../tech-stack.md). This service
also uses the following:

| Need | Choice | Why |
|---|---|---|
| Queries | DuckDB, over Arrow tables pyarrow reads | In-process SQL with no warehouse to run. DuckDB's own S3 access needs the `httpfs` extension, which it downloads on first use; pyarrow already reaches the bucket |
| Columnar I/O | pyarrow | Already used for export |
| Judge calls | `httpx2`, against model-gateway's OpenAI-compatible API, with its wire models | `openai` may be imported only in model-gateway; `httpx2` is already the worker's client |

## Not settled

1. RPC names of `AnalysisService`.
