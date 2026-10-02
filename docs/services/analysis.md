# analysis

Python. It analyzes and scores runs that have already been exported. It covers four things:
queries, searching tool calls, rule scans added after a run, and LLM judge verdicts. It is a peer
of the Control API behind [edge](edge.md), not part of it. Its place among the services is in
[architecture.md](../architecture.md).

**Status:** the `report` job is built (`swarmeval.analysis`): trigger rates per case, variant,
and scorer from run summaries. Rule scans, event-rule scorers, and the LLM judge are not built
yet; M1 runs them as batch jobs too. M4 turns analysis into a gRPC service behind edge. Items marked *(proposed)* go beyond what the specs
decided; they are listed under [Not settled](#not-settled).

## Inputs

Exported runs in object storage: `events.parquet` per run and the run summaries
([event-log.md](../event-log.md#export)). pyarrow reads them through the S3 API and DuckDB
queries the Arrow tables in process, so no DuckDB extension is downloaded at run time. analysis
never reads the `runs` schema and never writes object storage.

## Outputs

Derived results go to their own `analysis` schema in the shared Postgres: rule matches, judge
verdicts, and offline scores, each keyed by `run_id` and `event_id`. analysis owns that schema.
Events and exports are never rewritten, and replay and audit always read the originals
*(schema proposed)*.

## Capabilities

| Capability | How |
|---|---|
| Queries | SQL over Parquet views (`events`, `runs`). `swarm query` passes arbitrary SQL. The console offers filters on tool, agent, run, time range, and tags instead |
| Rule scans | User-defined keywords, regexes, and rule sets, run over payloads after the fact. Payloads are first decoded where they look like base64, gzip, XOR, or combinations of them, and then matched. Matches become tags in the `analysis` schema |
| Event-rule scorers | The case's rule scorers, sharing their detector interface with the online Monitor. Output is an Inspect `Score` with `1 = triggered` |
| LLM judge | Asks a question about a run or a range of its events, through [model-gateway](model-gateway.md). The verdict must cite `event_id`s. Before storing it, analysis checks that every cited id exists in that run, and a verdict with no valid citation is rejected |
| Reports | Trigger rate per case, variant, and scorer over `done` runs: epochs, mean, stderr, and a 95% Wilson interval, which, unlike mean ± 1.96·stderr, does not collapse to a point when no epoch or every epoch triggered. Runs that ended otherwise are listed per status and left out (runtime spec Q6). Built as the `report` job. Grouping by risk category, isolation level, fidelity, and `reasoning_visibility` is not built |

Each capability is a job: `python -m swarmeval.analysis <job>`, with the object store flags the
other entry points take. Built so far:

```bash
uv run python -m swarmeval.analysis report --s3-endpoint 127.0.0.1:9000 --s3-scheme http \
  --submission c46c60b8   # repeatable; without it, every run in the bucket
```

It prints a Markdown table.

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
| Judge calls | `httpx`, against model-gateway's OpenAI-compatible API | `openai` may be imported only in model-gateway, and httpx is already a dependency of it *(proposed)* |

## Not settled

1. An `analysis` schema for derived results.
2. RPC names of `AnalysisService`.
3. Whether to reuse `inspect-scout` for scans and judges (runtime spec Q8), and whether Docent's
   format becomes the main export for multi-agent analysis (runtime spec Q9).
