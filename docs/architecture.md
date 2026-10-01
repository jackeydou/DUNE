# Architecture

How SwarmEval is put together: which services exist, what each one owns, how they talk, where
data lives, and how the pieces are isolated. Only the project skeleton is built so far; the
**Status** column says which milestone brings each service; the milestones are defined in the
v1 spec §9. Why it is shaped this way is in the
[v1 spec](../spec/2026-09-27-swarmeval-v1/README.md) and the
[runtime spec](../spec/2026-09-28-runtime-sandbox-logs/README.md). Decisions the runtime spec still lists as
open questions are marked *(open, runtime spec Q…)* with the question's number. This page covers
what crosses service boundaries. Each service's internals, interfaces, and libraries are on its own
page under [services/](#services); the event format and the `runs` tables are in
[event-log.md](event-log.md); shared libraries are in [tech-stack.md](tech-stack.md).

## Services

Services that touch the operating system, network, containers, or the platform are written in Go.
Services that touch models, agents, or evaluation semantics are written in Python, so the case
models, the `inspect_ai` mapping, and case hooks exist only in Python.

| Service | Language | Owns | Status |
|---|---|---|---|
| [`orchestrator`](services/orchestrator.md) | Python | Control plane: run queue, scheduling, leases, Control API. Run workers: agent loop, Message Bus, online Monitor, token budgets, event sequencing and writes. Case loading and validation | M0 |
| [`model-gateway`](services/model-gateway.md) | Python | Provider adapters, OpenAI-compatible API, recording every model call, per-key rate limits | M0 |
| [`sandboxd`](services/sandboxd.md) | Go | Sandbox lifecycle through the docker or k8s API; runs tool calls inside sandboxes and reports the file diff and surviving processes after each one | M0 |
| [`net-gateway`](services/net-gateway.md) | Go | One instance per run: TLS interception, DNS, network policy, pcap | M1 |
| [`analysis`](services/analysis.md) | Python | DuckDB queries, rule scans, LLM judge, offline scorers over exported runs | M2 as batch jobs; service in M4 |
| [`edge`](services/edge.md) | Go | The only public entry: authentication, tenants, workspace authorization, console backend | M4 |
| [`swarm` CLI](services/edge.md#swarm-cli) | Go | Thin client of `edge` | M4 |
| [Web console and replay](services/edge.md#console) | TypeScript | Browser UI, served through `edge` | M4 |

Until M4 there is no public client. Runs are triggered through the orchestrator's gRPC Control
API (integration tests, grpcurl), which listens on the internal network only and has no
authentication until `edge` exists.

## How they talk

Blue is Go, yellow is Python.

```mermaid
flowchart TB
    WEB["Console Web"] & CLI["swarm CLI · Go"] -- HTTP/JSON --> EDGE

    subgraph PLATFORM["Platform services: unreachable from sandboxes, gRPC + mTLS between them"]
        EDGE["edge · Go<br/>auth · tenants · workspace authz · console backend"]
        subgraph ORCH["orchestrator · Python"]
            CP["control plane<br/>run queue · scheduling · leases<br/>Control API"]
            W["run worker<br/>agent loop · Message Bus · Monitor<br/>event sequencing and writes"]
        end
        AN["analysis · Python<br/>DuckDB · rules · LLM judge"]
        MG["model-gateway · Python<br/>provider adapters · recording · rate limits"]
        SBX["sandboxd · Go<br/>sandbox lifecycle · tool execution"]
    end

    subgraph RUN["One isolated network per run"]
        BOX["sandbox containers<br/>tool calls run here"]
        NG["net-gateway · Go (one per run)<br/>TLS interception · DNS · policy · pcap"]
        HP["honeypots / mock services"]
    end

    LLM["model inference<br/>vLLM / SGLang / API"]

    EDGE --> CP & AN
    CP -- assigns run --> W
    W -- model calls --> MG
    W -- create sandbox · run tool --> SBX
    SBX -- docker / k8s API --> BOX
    BOX -- all egress --> NG
    NG --> HP
    AN -. judge .-> MG
    MG --> LLM

    classDef go fill:#d7f0f7,stroke:#00758f,color:#0b2a33
    classDef py fill:#fbeecd,stroke:#b8860b,color:#3a2a00
    class EDGE,SBX,NG,CLI go
    class CP,W,AN,MG py
```

- Services talk gRPC with mTLS. Contracts live in `proto/` and are managed with buf; `edge`
  serves HTTP/JSON to browsers and the CLI through Connect. `net-gateway`'s event stream to its
  run's worker uses per-run certificates from M1; the other services get mTLS in M4, with `edge`.
  Until then they talk plain gRPC on the internal network.
- Each service owns its data. Another service asks it over gRPC and never reads its tables.
- The split is by function, never by run. All agent loops of one run, its sequencing, and its
  writes stay in one worker. Different runs spread across workers.

## Event flow

```mermaid
flowchart LR
    MG["model-gateway · Python<br/>model calls"] & NG["net-gateway · Go<br/>network requests · DNS"] == gRPC stream, ack after commit ==> W["run worker · Python<br/>sequencing · hash chain<br/>committed with agent state"]
    SBX["sandboxd · Go<br/>tool result · file diff · process snapshot"] -- returned with the tool result --> W
    W --> PG[("Postgres<br/>runs schema<br/>evidence while running")]
    PG -- export at run end --> OBJ[("object storage<br/>.eval · Parquet")]
    OBJ --> AN["analysis · Python<br/>offline analysis · scorers"]
    PG -. LISTEN/NOTIFY .-> CP["orchestrator control plane · Python"]
    CP -- gRPC stream --> EDGE["edge · Go<br/>live events for the console"]

    classDef go fill:#d7f0f7,stroke:#00758f,color:#0b2a33
    classDef py fill:#fbeecd,stroke:#b8860b,color:#3a2a00
    class NG,SBX,EDGE go
    class MG,W,AN,CP py
```

- Events come only from the gateways and `sandboxd`, never from the agent.
- The run's worker is its only writer. It assigns the run-wide `seq`, extends the hash chain, and
  commits each event in the same transaction as the agent state it changes. Only then does it ack,
  and only after the ack does the gateway let the call through. With no worker to ack, the gateway
  refuses the call (fail closed), so no model call or network request goes unrecorded *(open,
  runtime spec Q5)*.
- Network events are group-committed about every 100 ms and acked after the commit, so a request
  that was let through is never lost *(open, runtime spec Q2)*.
- File and process events are collected by `sandboxd` after each tool call: a diff of the key paths,
  which are mounted as host volumes, and the processes still alive. They come back with the tool
  result and commit in the same transaction as its `ToolEvent`. Reads that write nothing are not
  recorded; a decoy file counts as hit when its canary content shows up anywhere. Syscall-level
  audit is an opt-in interface backed by gVisor Runtime Monitoring.
- Content reaches an agent's context only after the transaction that records it has committed.
- Events are stored as serialized Inspect `Event`s, with SwarmEval fields under
  `metadata.swarmeval`. At run end they are exported as a standard `.eval` log and as Parquet.
  Format, hash chain, and export layout: [event-log.md](event-log.md).

## Storage

| Store | What it holds | Owner |
|---|---|---|
| Postgres `tenant` schema | Users, tenants, workspaces | `edge` |
| Postgres `control` schema | Run queue, run status, leases (`owner_id`, `lease_until`, `owner_epoch`) | `orchestrator` |
| Postgres `runs` schema | `events`, `messages`, `agent_state`, `extension_state`, `deliveries`, `sandboxes` for every run, keyed by `run_id` ([event-log.md](event-log.md#tables)) | `orchestrator` |
| Postgres `analysis` schema *(proposed)* | Derived results: rule matches, judge verdicts, offline scores | `analysis` |
| Object storage | Exported `.eval` and Parquet (with hash chain fields); large blobs such as pcaps and file snapshots, content-addressed | written by `orchestrator`, read by `analysis` |

Object storage is always reached through the standard S3 API, on every deployment, and no
implementation-specific feature is used, so the backing store can be swapped by configuration.
The export bucket is created with object lock enabled. `analysis` queries the Parquet files in
place with DuckDB's `httpfs` extension.

- The rows in `runs` are the evidence original; exports are derived from them. Derived results
  such as later rule matches or judge verdicts go to separate tables and never rewrite `events`.
- An agent's context at step *k* is read straight from `agent_state` and `messages`. Recovery
  and fork use the same query; nothing is rebuilt by replaying events.
- `control` and `runs` must share one Postgres instance: each write transaction checks
  `owner_epoch` in `control.runs`, so a stale worker cannot write after a takeover.

## Isolation

- Ingress and egress are denied by default.
- Each sandbox sits on its own network whose only other member is its run's `net-gateway`, which
  holds the network's gateway address. The host has no address there, so platform services,
  Postgres, and object storage give a sandbox nothing to connect to, and with `net-gateway` down
  a sandbox reaches nothing at all. How docker is set up for this:
  [services/net-gateway.md](services/net-gateway.md#topology).
- `net-gateway` accepts TCP and UDP to any address transparently (TPROXY in its own namespace), so
  a connection straight to an IP is recorded like any other. Honeypots and mock services sit on a
  separate network that only `net-gateway` reaches. On the platform side, `net-gateway` has one
  connection: the event stream with its run's worker, over a certificate that can write that run
  alone. Detail: [services/net-gateway.md](services/net-gateway.md).
- The agent loop runs outside the sandbox. Containers only execute tool calls, so model keys,
  logs, and recovery state are out of the agent's reach.
- Sandboxes run on gVisor (`runsc`) by default and fall back to runc where gVisor is missing.
  Each run records the isolation level it actually got.
- Before each batch, a probe checks that sandboxes cannot reach the host, platform services, or each
  other, and that a connection to an arbitrary IP shows up as a `net.*` event.

## Failure handling

| Failure | What happens |
|---|---|
| A worker dies | Another worker, or the same one after restart, takes the run over once its lease expires. `owner_epoch` goes up by one, and running sandboxes are claimed by their labels instead of being recreated |
| `model-gateway`, `net-gateway`, or `sandboxd` is unavailable | The run pauses and its sandboxes are frozen; no connection error is handed to the agent. On recovery a `CheckpointEvent` records the pause, and paused time does not count toward timeouts. A pause over the configured limit marks the run interrupted and schedules a new epoch |
| A step was half done | A model request whose response was not committed is resent and sampled again; the agent never saw the lost response, and the recovery `CheckpointEvent` notes it. `model-gateway` keeps no response cache. A tool call without a result is not rerun; the agent sees an `InterruptEvent` |

Every recovery records a fidelity level per sandbox: `exact` (only the orchestrator restarted),
`fs_preserved` (files kept, processes lost), or `lost`. A case can set the minimum it accepts
*(open, runtime spec Q6)*.

## Deployment

Single machine and k8s run the same services from the same images, released together under one
version. The differences are confined to the rows below and sit behind driver interfaces. M0–M4
build the single-machine column; the k8s column arrives in M5.

| | Single machine (physical or VM) | k8s |
|---|---|---|
| Orchestration | docker compose | Helm chart |
| Sandbox driver | docker API, gVisor | k8s API: Pod + RuntimeClass (gVisor) |
| Network isolation | One network per sandbox, shared only with the run's `net-gateway`, which holds the gateway address | To be decided: NetworkPolicy cannot reroute, so the Pod needs an in-namespace redirect to `net-gateway` *(open, runtime spec Q12)* |
| Postgres | Container in compose, on a persistent volume | Managed service or an operator |
| Object storage | RustFS container in compose, on a persistent volume | RustFS or a cloud S3 service |
| Service certificates | Self-signed CA generated at start | cert-manager |
| Run takeover | On the same machine | On any node; sandbox Pods outlive workers *(open, runtime spec Q4)* |

On a shared server, SwarmEval must not disturb other workloads: it does not restart the docker
daemon, does not change global iptables, and only cleans up resources it labeled. That rules out
k3s there.
