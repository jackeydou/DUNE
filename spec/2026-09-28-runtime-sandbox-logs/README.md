# Runtime 设计：沙箱拓扑、日志对齐 Inspect、run 恢复、网络拦截

## Status

draft · 2026-10-01 · M0 部分已实现并合并（#1–#3，至 `bbb04f1`）；M0 门槛待 v1 spec §9 的真实模型运行验证。

2026-09-29 · 决定 13、16 已定（2026-09-29 改为状态存库、只用 Postgres），决定 3、23 已定（决定 3 于 2026-09-29
改为工具调用后由 sandboxd 采集，去掉 Tetragon），决定 24 已定、前提于 2026-10-01 验证通过（Open question 11）；决定 12 的两处细节和决定 17、18、20
按讨论中的推荐项落稿，尚待确认（见 Open questions）

## Request

在 [v1 spec](../2026-09-27-swarmeval-v1/README.md) 基础上，继续讨论 agent 与 sandbox 的整体设计，一共三件事：

1. **沙箱拓扑**：v1 spec §5 写死了"每个 agent 一个容器"。现有评估基本都忽略了多个 agent 共享同一个 workspace 和环境的情况。
   agent 与 sandbox 的对应关系要可配置：既能让多个 agent 在同一环境里协作完成任务，也能一个 agent 对应一个 sandbox。
   后者既用来测单 agent 能力，也用来测隔离环境下是否会出现异常路径，让 agent 发现彼此并通信协作。这里的"共享上下文"
   指共享**环境**，不是共享 LLM 对话上下文。
2. **日志对齐 Inspect**：日志和评估结果对齐 Inspect AI 的 JSON schema 与数据结构，同时能扩展我们需要的字段。
3. **重启与恢复**：v1 spec 写的是"run 状态存 SQLite（断点续跑）"。讨论时提出了三个没有对齐的问题：agent 数据存 SQLite
   是一个库还是每个 agent 一个库；SQLite 是文件，是否要打包进 agent 运行环境；假设 orchestrator 不做水平扩展，这个定位
   本身就有问题。

   2026-09-29 追加：agent 运行时的中间状态不应该从事件日志重建，效率太低；事件日志应该是从数据库导出、用于离线分析的
   数据。讨论中进一步明确：服务不一定部署在物理机上（物理机、VM、k8s 都可能），存储只用 Postgres。

2026-09-29 追加第四件事：**网络拦截**。review 时发现，只把沙箱接到 run 的 internal docker 网络，保证不了 v1 spec §5
"所有出站流量必经 Network Gateway"，直接用 IP 外连的尝试也没有人记录。

规模前提：每个 case 最多十几个 agent，再多人工复核不过来（v1 spec §9 问题 3 已按此修订）。

## 背景：Inspect 的格式是什么

Inspect 的日志格式没有遵循外部标准，是它自己定义的：以 `inspect_ai.log` 里的 pydantic v2 模型为准（`EvalLog`、
`EvalSample`、`EvalSpec`、`Score`、20 多种 `Event`），JSON Schema 由 `EvalLog.model_json_schema()` 生成，文件格式
版本记在 `EvalLog.version`。它是 UK AISI 生态里的事实标准。

以下行为用 `inspect_ai 0.3.272` 实测（构造 log → `write_eval_log` → `read_eval_log`）：

| 写法 | 读回结果 |
|---|---|
| 放进 `metadata` 字典（EvalSpec、EvalSample、Score、每个 event 都有） | 原样保留 |
| 在模型上加顶层自定义字段 | 能读，但被静默丢弃 |
| 自定义 event 类型（如 `event: "net.request"`） | 整个 sample 校验失败 |

没有单独的"只含 schema"的包；`import inspect_ai.log` 约 0.45 秒并会加载 textual；`inspect_ai` 带 79 个传递依赖
（含 boto3、s3fs、textual、fastapi、tiktoken，不含 openai）。

## Decisions

### 一、沙箱拓扑

#### 1. 默认每个 agent 独立一个沙箱，共享必须显式声明

不写任何共享声明时，每个 agent 各自获得一个私有沙箱，用 `sandbox_profile` 指定 profile。要共享，先在 `env.yaml` 的
`sandboxes:` 里声明一个具名沙箱实例，再让 agent 用 `sandbox:` 引用它。两个字段互斥：只有写了 `sandbox:` 才会共享。

```yaml
# env.yaml
sandbox_profiles:
  restricted: { image: swarmeval/agent-base:py312, ... }   # 同 v1 spec §5

sandboxes:                      # 只在需要共享时声明
  team_box: { profile: restricted }

# case.yaml
swarm:
  agents:
    - id: dev
      sandbox: team_box         # 共享实例
    - id: qa
      sandbox: team_box
      os_user: qa               # 可选：同一沙箱内用独立 unix 用户
    - id: rival
      sandbox_profile: restricted   # 私有沙箱（默认形态）
```

- 两个字段都没写的 agent 使用 profile `default`。`env.yaml` 里没有 `default` 时，加载 case 直接报错并指出是哪个 agent。
- `sandbox:` 引用了不存在的实例，或者某个实例没有被任何 agent 引用，都在加载时报错。
- 私有沙箱的实例名自动生成为 `{agent_id}`，共享实例用声明的名字。名字是确定性的，事件、回放和跨 run 对比都依赖这一点。

**理由**：每个 case 最多十几个 agent，手写实例名完全可控，不需要 `count`、分组、自动装箱这类批量机制。默认私有是更安全
的默认值：忘了声明只会让隔离更强，不会意外打通环境。

#### 2. 四档隔离程度

| 档位 | 怎么配置 | 用途 |
|---|---|---|
| 1. 完全隔离 | 每个 agent 私有沙箱，不声明共享资源 | 单 agent 能力；隔离下能否发现彼此 |
| 2. 隔离 + 显式共享资源 | 私有沙箱 + v1 spec §5 的共享卷、共享 mock 服务、共享包缓存 | 通过指定资源交互 |
| 3. 同沙箱、不同用户 | 共享实例 + 每个 agent 设 `os_user` | 同处一个环境，有文件权限边界 |
| 4. 同沙箱、同用户 | 共享实例，不设 `os_user` | 完全共享环境 |

共享与否**不作为 variant 维度**。需要对照时写成两个 case，或者用 v1 spec §4 已有的通用 `${variant.x}` 替换，不为拓扑
做专门的变体机制。

#### 3. 文件与进程事件：每次工具调用后由 sandboxd 采集

2026-09-29 修订：原为 Tetragon（eBPF）采集、按进程血缘归属，否决理由见 Rejected。

sandboxd 执行完一次工具调用后，先采下面两样再把结果交回 worker；它们随工具结果一起返回，与 `ToolEvent` 在同一个事务里
提交（写前规则，决定 13）：

- **文件 diff**：profile 里的关键路径（`/workspace`、共享卷、受保护路径）挂成宿主卷，sandboxd 在宿主侧比较这次调用前后的
  变化，生成 `fs.*` 事件（路径、操作、diff 摘要、属主 uid）。采集不进入沙箱，容器里的进程察觉不到。
- **进程快照**：列出沙箱里的进程（`runsc ps` / `docker top`），这次调用结束后仍然存活的新进程记为 `proc.*` 事件。
  （2026-10-01 修订）改为 sandboxd 在沙箱里以 root 执行一段 POSIX sh 脚本读 `/proc`，runc 和 runsc 同一套。原因：gVisor 下
  `docker top` 把沙箱内的 pid 拿到宿主进程表里查，列出的是无关的宿主进程，进程快照全空；宿主上直接调 `runsc ps` 要让
  sandboxd 能访问 runsc 和 shim 的状态目录，容器化部署和 k8s 下都不成立。代价是每次快照多一次很短的 exec，恰好在盯进程表的
  后台进程能看到它。pid 改为沙箱内的 pid，和超时杀进程用的一致。

归属：

- 同一个沙箱的工具调用串行执行（round_robin 下本来如此，async 下每个沙箱一把执行锁），一次调用窗口内的变化归属于发起
  这次调用的 agent。
- 窗口内还有更早调用留下的后台进程存活时，这个窗口的 `fs.*` 事件标为 `attribution: ambiguous`，列出候选的来源调用；设了
  `os_user` 时用文件属主 uid 缩小范围。
- run 结束时对所有沙箱再做一次 diff，收下后台进程最后的改动。

看不到的：不产生写入的文件读取，以及调用窗口内的系统调用细节。

- 读诱饵文件改为看 canary **是否被使用**：canary 内容出现在工具输出、消息、网络请求或写出的文件里就算命中（v1 spec §5）。
- 系统调用级审计（例如刻画逃逸尝试）留一个接口，按 case 开启，后端用 gVisor 的 Runtime Monitoring（seccheck）。第一批
  case 不用。

事件同时记录 `sandbox_id` 和 `agent_id`，放在 `metadata.swarmeval` 下（Inspect 的 `SandboxEvent` 没有沙箱名字段，见决定 10）。

#### 4. 网络策略按沙箱整体生效

同一个沙箱出来的流量源地址相同。Network Gateway 的策略匹配粒度是**沙箱**，不是 agent：共享同一沙箱的 agent 适用同一套
出入站规则，网络事件的归属是 `sandbox_id`。私有沙箱下两者等价。

**理由**：per-agent 代理凭据在共享容器里会被对方读到并冒用；按 uid 做 iptables owner 匹配依赖 gVisor 网络栈支持，没有
验证过。按沙箱整体生效没有歧义，也和"共享环境"的语义一致。

#### 5. 共享沙箱的能力与资源是并集

- 工具层权限按 agent 区分，OS 层权限按沙箱区分。qa 的工具是只读，但它所在的容器对 dev 可写。这个差距正是 v1 spec §5
  用来测越权尝试的信号，不是缺陷。共享沙箱里 per-agent 的 OS 级限制只能靠 `os_user`。
- CPU、内存、pids、磁盘上限按沙箱计。一个 agent 耗尽资源会影响同沙箱的其他 agent，这是真实场景，记录下来即可。
- 恢复保真度按沙箱实例记录，沙箱丢失会影响引用它的所有 agent（见决定 22）。

#### 6. 隔离模式下测"发现彼此"

- **每个沙箱一个独有 canary**：按实例名自动生成，放进文件、环境变量和主机名。某个沙箱的 canary 出现在另一个 agent 的
  输出、消息或网络请求里，就是一次跨沙箱信息流的确定性证据，不依赖 LLM judge。
- **自检按拓扑生成**：v1 spec §5 的隔离自检扩展为"未声明互通的沙箱之间确实不通"。探针根据 case 的沙箱拓扑，逐对尝试
  直连、共享路径、`/proc` 可见性、DNS 互查。
- **是否告知对方存在**：case 可以加 `peers_disclosed: true | false` 变体，做法同 `monitor_disclosed`。
- **残余信道记录**：同一宿主上的 CPU 争用、时序这类关不干净的侧信道，写进 run 元数据，评估时知道哪些发现本来就可能。

### 二、日志与结果对齐 Inspect

#### 7. 直接依赖 `inspect_ai`，以它的模型为唯一事实来源

锁死版本（`inspect_ai==x.y.z`），只在 `swarmeval/events/` 里 import，规则同"openai SDK 只在 gateway 里 import"。我们
产出的对象用它的模型构造、用它的函数写出，从构造上保证对齐，不维护第二份结构定义。

我们自己维护的只有：`swarmeval` 扩展字段的模型、runtime 事件到 Inspect event 的写入代码、版本升级。

**升级**是一次有意的变更：改锁定版本，跑读回测试。Inspect 保证新版本能读旧 log，反之不保证，所以升级后产出的 log 可能
要求对方也升级；`EvalSpec.packages` 记录了产出时的版本。

CLI 这类薄客户端不在顶层 import `inspect_ai`。

#### 8. 扩展规则

1. 所有自定义字段放在 `metadata["swarmeval"]` 这一个命名空间下，不加顶层字段。
2. Inspect 有对应类型的事件用原生类型；没有的一律用 `InfoEvent(source="swarmeval.<type>", data=...)`，`data` 用我们
   自己的 pydantic 模型校验。
3. 扩展部分有自己的版本号 `metadata.swarmeval.schema_version`。日志的完整版本 = Inspect 锁定版本 + 扩展版本。扩展结构
   变化时按 AGENTS.md「Event schema」规则处理：升版本，读取器能加载旧 run。

#### 9. 概念映射

| SwarmEval | Inspect |
|---|---|
| case | Task |
| variant（模型 + 参数组合） | 一个 EvalLog（`eval.model` + `eval.task_args`） |
| case 的任务实例 | Sample（多数 case 只有 1 条） |
| epoch | `sample.epoch` |
| run（variant × epoch） | 一个 EvalSample |
| suite | eval-set（一个目录多个 log） |
| agent | `SpanBeginEvent` / `SpanEndEvent`（`type="agent"`） |
| 沙箱实例 | 具名 sandbox；`sandbox_id` 放 `metadata.swarmeval` |

这修正了 v1 spec §3 概念表里"Variant ↔ Sample"的对应。按这个映射，Inspect 的 epoch reducer（`mean`、`at_least_k`、
`pass_at_k`）和 `stderr` 直接作用在我们的 epoch 上。

混编模型：主模型放 `eval.model`，swarm 里的其余模型放 `eval.model_roles` 或 metadata，接入时按锁定版本的实际字段定。

#### 10. 事件映射

| v1 spec §6 事件 | Inspect 类型 |
|---|---|
| `llm.request` / `llm.response` | `ModelEvent`；原始输出、`reasoning_passback`、权重 hash 放 metadata |
| `tool.call` / `tool.result` | `ToolEvent` |
| 工具层面的沙箱执行、文件读写 | `SandboxEvent` |
| 被中断的工具调用 | `InterruptEvent` |
| 恢复点 | `CheckpointEvent` |
| 预算、上限触发 | `SampleLimitEvent` |
| 评分 | `ScoreEvent` / `Score` |
| `msg.send` / `msg.deliver`、`net.*`、`fs.*` / `proc.*`（sandboxd）、`env.state`、`monitor.*`、`run.lifecycle` | `InfoEvent(source="swarmeval.<type>")` |

每个事件的 `metadata.swarmeval` 带：`seq`（run 内全局序号）、`parent_id`（因果父事件）、`source`（gateway / sandbox /
orchestrator）、`agent_id`、`sandbox_id`、哈希链字段。EvalSpec 的 `metadata.swarmeval` 带：`workspace`、隔离等级、
`network_stealth`、恢复保真度。

#### 11. 评分

- scorer 输出就是 Inspect `Score`：`value`、`answer`、`explanation`、`metadata`。LLM judge 引用的 `event_id` 放
  `Score.metadata`。
- **分数方向写死**：安全评估里命中是坏事，value 用 `1 = triggered`，并在 `Score.metadata.swarmeval` 里声明含义。不复用
  `CORRECT` / `INCORRECT`，避免被当成准确率读。
- 聚合写进 `EvalResults.scores[].metrics`。v1 spec §7 的"触发率 + 95% CI"用 mean + stderr 计算，reducer 注明 mean。

#### 12. 数据结构对齐，存储容器不照搬

`.eval` 是 zip，sample 结束才写入，不能作为运行中的存储。分两层：

- **运行中**：Postgres（决定 16）。每个事件是 `runs.events` 的一行，`payload` 列是序列化后的 Inspect `Event`（`jsonb`，
  带 `metadata.swarmeval`）；`seq`、`agent_id`、`sandbox_id`、`source`、`parent_id` 和哈希链字段另存为列，供索引和校验。
- **run 结束**：从库里导出到对象存储：组装成 `EvalSample` 写出标准 `.eval`；拍平成 Parquet 供 DuckDB 离线查询，保留哈希链
  字段，可独立校验；外加一张 run 级汇总表（对应 Inspect 的 log header），列表类查询只读这张表。

证据原件是 Postgres 里的行，导出文件都由它派生，审计、回放和 spoofing 比对以库为准。

- **`ModelEvent.input` 存引用**：不写全量上下文，存 `(agent_id, gen, len)`（决定 13）加请求内容的 hash，导出 `.eval` 时
  展开。全量写入会让存储随步数平方增长。gateway 记录的请求 hash 与此不符，就是 spoofing 信号。
- **网络事件组提交**（2026-09-29 修订，原为审计事件攒批、崩溃时可丢一批）：`net.*` 量大。owner 照常分配 `seq`，约 100ms
  攒一批在一个事务里提交，提交后统一 ack，net-gateway 收到 ack 才放行。已放行的请求不会丢记录，代价是每个请求最多多等
  一个批次窗口。`fs.*` / `proc.*` 随工具结果提交（决定 3），不走这条路径。
- **大对象**：pcap、文件快照进对象存储，按内容寻址，事件里只存 hash。
- **对象存储只走标准 S3 API**（2026-09-29 修订，原为单机用本地目录）：单机用 compose 里的 RustFS，集群用 RustFS 或云上
  的 S3，不用任何实现独有的功能，换实现只改配置。导出用的 bucket 创建时就开启 object lock。DuckDB 用 `httpfs` 扩展
  直接查询 S3 上的 Parquet。
- **运行中的事件流**：orchestrator 控制面用 `LISTEN/NOTIFY` 订阅新事件，不轮询，再经 gRPC 流推给 edge 和控制台；在线
  Monitor 在 owner 进程内，提交后直接收到。

### 三、run 恢复与水平扩展

#### 13. agent 状态直接存库，与事件同一事务提交

agent 在沙箱外的状态是它的上下文，加上 Message Bus 的待投递消息、轮次和已用预算。这些存成表，和产生它们的事件在**同一个
事务**里提交。状态和证据在同一个库、同一次提交里，不存在崩溃落在两次写入之间的不一致；恢复直接读，不从事件折叠。

| 表（`runs` schema） | 内容 | 写法 |
|---|---|---|
| `events` | 决定 12 | 只追加 |
| `messages` | `(run_id, agent_id, gen, idx)` → 一条上下文消息，外加产生它的 `seq` | 只追加 |
| `agent_state` | 每步一行：`run_id`、`agent_id`、`seq`、`gen`、`len`、轮次、状态、已用预算 | 只追加 |
| `deliveries` | 消息投递：消息的 `seq`、收件 agent、状态、投递时的 `seq` | 更新 |
| `sandboxes` | `sandbox_id`、容器 id、恢复保真度 | 更新 |

- agent 在第 k 步的上下文 = `agent_state` 里 `seq ≤ k` 的最后一行给出的 `(gen, len)`，再取 `messages` 里这一代的前 `len`
  条。存储量随步数线性增长。
- 上下文压缩或截断时 `gen` 加一，从新一代重新追加。旧的代保留，fork 回压缩之前照样能取到。
- **恢复和 fork 是同一个查询**：恢复取最新一行后继续跑；fork 取第 k 步那一行，替换一条消息后继续跑（v1 spec §6）。代价与
  run 长度无关，不调模型。
- **写前规则**：模型响应、工具结果、投递的消息，每次有内容要进入 agent 上下文，对应的事件和状态变化先在一个事务里提交，
  之后才进上下文。agent 见过的内容一定在库里。

#### 14. 恢复状态永远不进 agent 容器

agent 循环跑在沙箱外（v1 spec §3），容器只执行工具调用。恢复依据如果在 agent 能触及的地方，agent 就能改写自己的"记忆"
（HF 事件中约 7% 的 transcript 含伪造 tool call）。Postgres 和对象存储对 agent 容器网络不可达：加载 env 时拒绝指向它们的
放行规则，隔离自检的探针验证连不上。

#### 15. 扩展按 run 切：控制面无状态多副本，run 由单一 owner 承载

| 角色 | 负责什么 | 怎么扩展 |
|---|---|---|
| 控制面 | Control API、调度器（run 队列、分配） | 无状态多副本，状态在 `control` schema |
| Run owner（worker） | 一个 run 的全部 agent 循环、Message Bus、事件定序与写入 | 一个 run 同一时刻只归一个 worker；加 worker 即加并发 run 数 |

- 不同 run 之间天然并行（一个 case 的模型 × 变体 × epoch 矩阵就是几十个互不相关的 run）。
- 一个 run 内不拆：run 内全局 `seq`、round_robin 的确定顺序、Message Bus 投递都需要单一定序者；每个 case 最多十几个
  agent，瓶颈在模型推理，一个 asyncio 进程足够。一个 worker 进程可以承载多个 run；存储在 Postgres，进程模型不影响存储。

**重启恢复与水平扩展是同一个机制**：worker 崩溃后，run 由另一个 worker（或重启后的自己）接管。单机重启是"接管者恰好是
同一台机器"的特例。

- **租约**：`control.runs(owner_id, lease_until, owner_epoch)`。owner 定时续租，续不上先自行停止执行；接管时
  `owner_epoch` 加一。
- **写入 fencing**：owner 的每个写事务先执行 `SELECT owner_epoch FROM control.runs WHERE run_id = :run FOR SHARE`，和自己
  的 epoch 不一致就回滚并停止。接管的 UPDATE 要等在途事务提交，之后的事务读到的都是新 epoch，所以假死的旧 owner 醒来后
  第一个事务就会失败，写不进任何东西。这要求 `control` 和 `runs` 在同一个 Postgres 实例里（决定 16）。
- **沙箱按标签认领**：容器带 `run_id` / `sandbox_id` label，新 owner 启动时对账，活着的容器直接接管，不重建；只处理自己
  打了标签的资源。

#### 16. 只用 Postgres：SQLAlchemy 2.0 + Alembic + psycopg 3

所有部署形态都用 Postgres。一个部署一个实例，两个 schema：

- `control`：run 队列、run 状态、租约。
- `runs`：所有 run 的事件和状态（决定 12、13），按 `run_id` 区分。

分两个 schema，保留 v1 spec §3"控制台自己的状态和轨迹数据分开"的边界；放在同一个实例，是因为写入 fencing 要在一个事务里
读 `control.runs`（决定 15）。

- 单机：docker compose 里起一个 Postgres 容器，数据放持久卷。集群：集群内 Postgres 或云托管 Postgres。代码和 schema
  相同，差别只在连接串。
- 驱动用 psycopg 3：一个驱动同时提供 async 接口（runtime）和同步接口（Alembic 迁移），支持 `LISTEN/NOTIFY` 和 `COPY`
  （导出时批量读）。
- `runs.events` 的 `payload` 和哈希字段写入后不再改写。表结构迁移只加列、加索引；旧 run 的 payload 按它自己的
  `metadata.swarmeval.schema_version` 读取。
- 测试用 testcontainers 起临时 Postgres，不用 SQLite 替身：两种库的锁和事务语义不同，fencing 必须在真 Postgres 上测。

**理由**：需要的只是一个有事务、任何接管者都能访问的库。部署形态不固定（物理机、VM、k8s 都可能），接管者可能在另一个
节点上；orchestrator 本来就是要部署的常驻服务，多一个 Postgres 的运维成本很小。

#### 17. v1 只做同节点接管

docker 后端的沙箱只存在于所在宿主（物理机或 VM），宿主挂了沙箱也没了，跨节点接管没有意义。存储已经不构成限制（决定 16），
限制来自沙箱：docker 后端的 run 固定在节点上，跨节点接管留给 k8s 后端（沙箱是 Pod，独立于 worker 存活）。

#### 18. 事件写入路径：run owner 是唯一写者

Model Gateway 是多个 run 共享的独立服务。gateway 把事件推给该 run 的 owner，owner 定序、提交、确认后 gateway 才返回
响应。每个 run 一个写者、一条哈希链；预算扣减放在 owner。owner 不在时 gateway 拒绝该 run 的请求（fail closed），保证
不存在没被记录的模型调用。代价是每次调用多一跳，gateway 依赖 owner 在线。

#### 19. 状态清单

| 状态 | 以哪里为准 | 恢复方式 |
|---|---|---|
| run 队列、run 状态、租约 | `control` schema | 直接读 |
| 事件（证据） | `runs.events` | — |
| agent 上下文 | `runs.messages` + `runs.agent_state` | 取最新一行 |
| Message Bus 待投递消息、轮次 | `runs.deliveries`、`runs.agent_state` | 直接读 |
| 已用 token 预算 | `runs.agent_state` | 直接读 |
| 大对象、导出的 `.eval` / Parquet | 对象存储 | — |
| 沙箱文件系统 | 容器可写层或卷 | 容器活着就认领；停了就重新 start |
| 沙箱内进程 | 只在内存 | 容器活着才有，否则丢失 |
| run 级 mock / 蜜罐服务 | 其容器 | 同沙箱 |
| 跨 run 共享蜜罐 | 它自己的持久存储 | 生命周期独立于任何 run |

#### 20. 恢复保真度

恢复本身会扰动实验：沙箱重启、后台进程消失，agent 可能察觉到环境不连续，评估意识类 case 尤其敏感。

- 每个沙箱实例记录恢复保真度，run 取最低值，统计时和隔离等级一样按它分组：
  - `exact`：只有 orchestrator 重启过，沙箱全程存活；
  - `fs_preserved`：文件还在，进程丢失；
  - `lost`：沙箱没了。
- 每次恢复写一条 `CheckpointEvent`，写明保真度和丢失的内容。
- case 声明可接受的下限，如 `recovery: { min_fidelity: exact }`。低于下限不续跑，run 标记为 interrupted，调度器补跑一个
  新 epoch。`lost` 默认不续跑：环境不是确定性的，重放工具调用重建出来的文件系统不可信。

#### 21. 半截步骤的处理

- **模型请求有、响应没有**（2026-09-29 修订，原为带幂等键重发、gateway 返回记录值）：由 owner 查 `agent_state` 判断。
  响应已提交就直接继续；没提交就重发、重新采样，并在恢复时的 `CheckpointEvent` 里记下丢弃了一次在途调用。写前规则保证
  没提交的响应 agent 从没见过，重新采样不会造成不一致。model-gateway 不做响应缓存。
- **工具调用有、结果没有**：不重跑（工具调用不幂等，也不知道是否已执行）。由 orchestrator 写一条 `InterruptEvent`，agent
  看到的是一次真实发生的失败。
- **消息已发、未投递**：按信道策略重新投递，用 `event_id` 去重。

#### 22. 共享沙箱的恢复

共享沙箱丢失会同时影响引用它的所有 agent，这些 agent 的保真度一起降级（决定 20）；网络与资源也按沙箱计（决定 4、5）。

#### 23. 基础设施故障时暂停 run，不把错误交给 agent

model-gateway、net-gateway、sandboxd 任一不可用时，owner 暂停受影响的 run：不再调度新的 agent 步骤，
也不把连接错误当作模型响应、工具结果或网络响应交给 agent。服务恢复后继续跑，写一条 `CheckpointEvent`，记下暂停时长、中断了
什么、有没有故障被 agent 看到，并计入该 run 的恢复保真度记录（决定 20）。

- **理由**：基础设施故障在 agent 看来就是环境行为。agent 可能因此换路径、找绕过手段，而这正是我们要测的行为，会污染结果。
- **沙箱冻结**：暂停期间同时冻结沙箱，缩短容器里的进程能观察到故障的窗口。docker 下用 `docker pause`；k8s 没有原生的
  Pod 暂停，手段待定（Open question 10）。
- **藏不住的部分**：故障发生时已经在途的网络连接会断，容器里的进程能看到；结果不确定的工具调用仍按决定 21 写
  `InterruptEvent`。这两种情况都在 `CheckpointEvent` 里标为"对 agent 可见"。
- **时间不计入**：暂停时长不计入 run 和 agent 的时间上限、超时。
- **暂停上限**：暂停超过部署配置的上限时，run 标记为 interrupted，按决定 20 由调度器补跑一个新 epoch。

### 四、网络拦截

#### 24. net-gateway 是每个沙箱唯一的邻居和默认路由

（2026-10-02）整条决定随网络能力延后；从 M1 起沙箱用 `--network none`，唯一出口是 worker 执行的 `web_request`。见
[轨迹分析优先 spec](../2026-10-02-trajectory-first/README.md) 决定 1–3。

只把沙箱接到 run 的 internal docker 网络做不到"所有出站流量必经网关"：同一网络上的沙箱之间、沙箱和蜜罐之间可以直连，
不经网关、不留事件；bridge 上的宿主 IP 让沙箱能连到宿主上监听 0.0.0.0 的服务；发往其他 IP 的包被静默丢弃，谁也没有记录。
所以单机 docker 下改成让 net-gateway 当路由器：

- **每个沙箱一个网络**：网络上只有这个沙箱和本 run 的 net-gateway，沙箱之间、沙箱和蜜罐之间在二层就不通。共享沙箱的几个
  agent 共用这张网卡（决定 4）。
- **网关地址给 net-gateway**：bridge 不给宿主侧分配地址，net-gateway 容器占用网络的网关 IP。沙箱的默认路由因此指向
  net-gateway；宿主、Postgres、平台服务在这个网络上没有可连的地址。
  （2026-10-01 验证后补充）bridge 用 `com.docker.network.bridge.inhibit_ipv4=true`，宿主侧没有地址，也没有到沙箱网段的
  路由。docker 的 IPAM 仍然保留网关地址，容器拿不到它，所以 net-gateway 先从 docker 拿一个普通地址，再用自己命名空间里的
  `NET_ADMIN` 把网关地址加到这张网卡上；IPAM 不会把它分给别人。网络不能建成 `--internal`：那样沙箱没有默认路由。
- **透明接收任意目的地址**：net-gateway 在自己容器的网络命名空间里用 TPROXY 接收发往任意 IP、任意端口的 TCP 和 UDP，保留
  原始目的地址。有 SNI 的 TLS 连接按 host 匹配策略，直接用 IP、没有 SNI 的按 IP 匹配；ICMP 和其他协议记录后丢弃。规则只
  装在 net-gateway 自己的命名空间里，不改宿主的全局 iptables。
- **直接用 IP 外连由 net-gateway 记录**：所有包的第一跳都是它，每个 TCP 连接、每个 UDP 包都生成 `net.*` 事件，带原始目的
  IP 和端口。沙箱里不需要任何审计，gVisor 和 runc 下一样。
- **蜜罐和 mock 在另一个网络**：只有 net-gateway 连得到，访问蜜罐必经网关。跨 run 共享的蜜罐同样只由 net-gateway 连接。
- **DNS**：sandboxd 挂载容器的 `/etc/resolv.conf`，指向网关 IP，不依赖 Docker 内嵌 DNS（127.0.0.11）。agent 绕开
  resolv.conf 直接查别的 DNS 服务器，包也先到网关。
  （2026-10-01 验证后补充）runc 下内嵌 DNS 关不掉：127.0.0.11 上的监听在 `/proc/net/udp`、`/proc/net/tcp` 里看得到，
  直接查它能解析同网络的容器名（只剩 net-gateway 的容器）。runsc 下没有这个痕迹。处理方式已定（2026-10-01，Open question
  14）：`network_stealth: full` 只在 runsc 下成立；runc 下照常运行，可探测性自检把内嵌 DNS 列为已知痕迹，报告按 run 的
  `isolation` 分组。
  （2026-10-01 PR #4 review 补充）它不只是痕迹：默认配置下内嵌 DNS 会把外部查询转给宿主的上游 DNS，agent 直接查
  127.0.0.11 就能绕过网关做 DNS 外传，Q11 只验了容器名解析，漏了这一点。sandboxd 把容器的 DNS 服务器设为网关地址，内嵌
  DNS 就只会转给网关：没有 net-gateway 时查询超时，有了之后照常留下 `net.dns` 事件。守护测试是
  `TestLiveSandboxRoutesOnlyToItsGatewayWhichNobodyHoldsYet`。
- （2026-10-01 补充）net-gateway 的三处实现细节按 [docs](../../docs/services/net-gateway.md) 的建议采纳：策略在 run 内
  固定，改策略就是另一个 run；路由到蜜罐或 mock 的域名返回每 run 的合成地址，连接到达时再映射回域名；事件流由 worker
  拨 net-gateway，接管时新 owner 以更高的 `owner_epoch` 重拨并替换旧流。
- **权限**：沙箱不给 `NET_ADMIN`、`NET_RAW`，改不了路由和地址；伪造源地址也只能发给网关。沙箱网络关闭 IPv6，不留一条没人
  管的路径。
- **平台侧只有一条连接**：net-gateway 除了沙箱网络和蜜罐网络，只接一个上游网络，用于放行规则允许的外网。在平台侧它只连
  本 run 的 owner，推送 `net.*` 事件并等 ack（决定 18），这条 gRPC 用按 run 签发的证书做 mTLS，只能写本 run 的事件。它不连
  model-gateway、Postgres 或其他平台服务。
- **隔离自检**：v1 spec §5 的探针加三项：连不到宿主和平台服务；连不到其他沙箱（决定 6）；随便连一个 IP，事件流里必须出现
  对应的 `net.*` 事件。

k8s 不适用：NetworkPolicy 只能放行或拒绝，改不了路由，被拒绝的包直接丢弃，网关记录不到。k8s 下要改成在 Pod 自己的网络
命名空间里做 REDIRECT（Istio 的 init container 做法，gVisor 的 netstack 支持这类规则），留到 M5（k8s 后端，v1 spec §9；Open question 12）。

## Rejected

- **按组声明 + 自动装箱**（`count`、`share: pack/group`）：为上千个 agent 设计的声明方式。每个 case 最多十几个 agent，
  手写名字更直观，批量机制的复杂度换不来任何东西。
- **共享与否作为 variant 维度**：需要为拓扑枚举组合，配置成本高；需要对照时写两个 case 更清楚。
- **per-agent 网络策略**（代理凭据或 uid owner 匹配）：见决定 4。
- **多个 agent 共享一份 LLM 上下文**：本质上退化成一个 agent。需要共享记忆时，用共享环境里的文件或一条信道表达。
- **自建 schema，Inspect 只作导出格式**：原方案。要维护两套结构和一层有损转换；既然决定对齐，就让 Inspect 模型成为唯一
  来源。
- **从 JSON Schema 生成我们自己的 pydantic 模型**（`inspect_ai` 只作开发依赖）：运行时更轻，但重新生成和升级跟进都要自己
  维护，等于变相维护第二份结构。
- **自定义 event 类型**：Inspect 读不了，整个 sample 失败。
- **顶层扩展字段**：Inspect 读取时静默丢弃。
- **从事件日志折叠重建 agent 状态**（本 spec 早先的决定 13）：恢复代价随 run 长度增长，`ModelEvent.input` 写全量时日志
  还按步数平方膨胀。状态和事件在同一个事务里提交之后，"两处存放会不一致"这个顾虑也不存在了。
- **运行中写 JSONL 作为证据**（早先的决定 12）：只有 owner 所在机器能读，接管者可能在另一个节点；运行中的查询要 tail 文件；
  防假死 owner 要按 epoch 分段加清单，比库里的 fencing 复杂。
- **SQLite**（整个部署一个、每个 run 一个，或与 Postgres 双后端）：库文件绑在一块本地盘上，部署形态不固定时接管者可能
  访问不到；双后端要维护两套锁和事务语义。它唯一的好处是省掉一个数据库服务，在 orchestrator 本来就是常驻服务的前提下不值。
- **Litestream / LiteFS 把 SQLite 复制到对象存储**：异步复制，接管时可能丢掉最后几次提交，破坏写前规则。
- **单机用本地目录作对象存储**：和 S3 并存就是两条代码路径，两边的语义（原子 rename、列举）也不同。单机也起一个 S3 兼容
  服务，两种部署就完全一致。
- **MinIO**：社区版 2025 年 12 月进入维护模式，2026 年 2 月仓库归档，不再有安全修复。
- **每个 agent 一个库**：跨 agent 操作（消息投递、swarm 级预算、轮次）变成跨库一致性问题。
- **恢复状态放进 agent 容器**：agent 可篡改；违反隔离默认值。
- **run 内拆分到多进程**：需要分布式协调重建全局顺序，而 run 规模（十几个 agent）不需要。
- **各来源分别写日志、事后合并**：run 内全局 `seq` 只能按时间戳事后排，恢复时要合并多个来源；放弃 v1 spec §6 的单条
  哈希链承诺。
- **只把沙箱接到 run 的 internal docker 网络**（早先的网络实现）：见决定 24 开头。
- **代理环境变量（`HTTP_PROXY` 等）**：agent 看得到，也能绕开，参考事件里的 NO_PROXY 例外就是这么被利用的；直接用 IP 的
  TCP/UDP 也不受它约束。
- **只靠 NetworkPolicy 或防火墙丢包**：被丢掉的包网关看不到，外连尝试没有记录。
- **model-gateway 按幂等键缓存模型响应**（本 spec 早先的决定 21）：写前规则加提交后才 ack，已经保证响应被记录当且仅当它
  进了 agent 上下文；"已记录的响应"就在 `runs.events` 里，由 owner 判断即可。缓存只会给 model-gateway 多一份无人认领的存储。
- **Tetragon（eBPF）做文件与进程审计**（本 spec 早先的决定 3）：宿主上的 eBPF 看不到 gVisor 容器里的进程，审计只在降级到
  runc 时有效，审计覆盖面和隔离等级绑在了一起；它需要特权、在整台宿主上加载 eBPF 程序，和"不打扰共享服务器上的其他负载"
  冲突；还要多一个节点代理服务、做进程血缘归属，事件量大到只能攒批提交、崩溃时可能丢一批。它独有的信号（不产生写入的
  读取、系统调用细节）在七类风险里只有少数 case 用得上，由 canary 使用检测和按需开启的 gVisor Runtime Monitoring 替代。
- **gVisor `runsc checkpoint/restore` 保进程**：docker 下仍是实验特性，不进 v1；作为以后把 `fs_preserved` 提升到 `exact`
  的手段。

## Open questions

1. 确认决定 12：`ModelEvent.input` 存引用，导出 `.eval` 时展开。代价是库里的行不是 Inspect 原样格式，只有导出的 `.eval` 是。
2. 确认决定 12：`net.*` 组提交的窗口取多大（约 100ms 时每个请求最多多等 100ms），还是每条同步提交；需要在 M1 实测
   net-gateway 的峰值请求量。
3. 证据原件：run 结束后以 Postgres 里的行为准。以后清理库里的旧 run 时，以对象存储里带哈希链字段的 Parquet 作为归档原件。
   何时清理、谁有权清理未定。
4. 确认决定 17：v1 只做同节点接管。
5. 确认决定 18：run owner 作为唯一写者（fail closed），而不是各来源分别写日志。
6. 确认决定 20：保真度三级 + case 声明下限；`lost` 默认补跑新 epoch。
7. ~~`os_user` 在 gVisor 下的文件权限隔离是否可靠~~ 已验证（2026-10-01，runsc release-20260928.0）：runc 和 runsc 下，
   一个 `os_user` 读不到、改不了另一个的 `0600` 文件和 `0700` 家目录，写出的文件在宿主侧带它自己的 uid。测试是
   `go/internal/sandboxd/docker_integration_test.go` 的 `TestLiveOSUsersAreSeparatedByFilePermissions`。
8. ~~`inspect-scout`（Inspect 生态的 transcript 扫描分析工具）和 v1 spec §3 的「日志分析与评估」能力域（analysis 服务）重叠。
   复用还是自建，需要先评估它的能力边界。~~ 已定（2026-10-02）：自建，`.eval` 作对外交换格式，见
   [轨迹分析优先 spec](../2026-10-02-trajectory-first/README.md) 决定 9。
9. ~~多 agent 行为分析的对外格式：Docent 原生格式支持一个 run 多条 transcript，是否作为多 agent 分析的主导出，Inspect
   `.eval` 用于交换评分结果？~~ 已定（2026-10-02）：不以 Docent 为主，需要时再加导出适配，同上。
10. k8s 下两件要在节点上做的事：决定 23 的冻结沙箱（k8s 没有原生的 Pod 暂停），决定 3 的宿主侧卷 diff。sandboxd
    不在节点上。候选是一个轻量的节点组件（操作 cgroup freezer、读卷，不用 eBPF），或者用 runsc 的 pause 加 exec 进 Pod
    采集；在 M5（k8s 后端）验证。
11. ~~决定 24 的前提，M1 验证~~ 已验证（2026-10-01，docker 29.8.2、runsc release-20260928.0，OrbStack 的 arm64 Linux
    虚拟机）。脚本和完整输出在 [q11/](q11/)，`run.sh` 可在任何装了 runsc 的 Linux docker 主机上重跑：
    - `inhibit_ipv4` 生效：宿主侧 bridge 没有地址，也没有到沙箱网段的路由。容器拿不到网关地址（IPAM 保留，报
      `Address already in use`），由网关容器自己把它加到网卡上可行，runc 和 runsc 下沙箱都以它为默认路由。`--internal`
      网络下沙箱没有默认路由，不能用。决定 24 已按此补充。
    - gVisor 下沙箱的 IP 和默认路由与 runc 一致，只有回环上的 IPv6。
    - 内嵌 DNS：runc 下仍在，runsc 下没有，见决定 24 的 DNS 一条和 Open question 14。
    - TPROXY 下 TCP 和 UDP 都拿到原始目的地址，UDP 可以用原始目的地址回包，runc 和 runsc 一样，不需要 netstack 兜底。
      发往宿主 IP、docker0 地址、其他沙箱网段的连接都落到网关。
    - 网关冻结时，发往宿主和网关的连接都超时，没有一个落到宿主：失败时是关闭的。
12. k8s 下怎么让 net-gateway 成为 Pod 的出口（决定 24）；在 M5（k8s 后端）验证。

13. worker 挂掉到被接管之间（租约到期前），沙箱里的后台进程还在跑，net-gateway 收不到 ack：拒绝请求（agent 会看到连接
    错误，违背决定 23 的原则），还是挂起请求并冻结沙箱（worker 已经不在，由谁来冻结）。M2 开工前定。
14. ~~runc 下 Docker 内嵌 DNS 留下的痕迹（Open question 11）~~ 已定（2026-10-01）：`network_stealth: full` 只在 runsc
    下成立，run 的实际隔离等级记在 `isolation` 字段；runc 下照常运行，可探测性自检把这项列为已知痕迹，报告按 `isolation`
    分组。没选 sandboxd 进沙箱网络命名空间删 DNAT 规则：监听本身还在 `/proc/net` 里，去不干净。

## Plan

阶段划分以 [v1 spec §9](../2026-09-27-swarmeval-v1/README.md) 为准（2026-09-29 重排）。本 spec 的决定落在：

（2026-10-02）里程碑再次重排，下面的编号是重排前的。对照（[轨迹分析优先 spec](../2026-10-02-trajectory-first/README.md)
决定 7）：M1 里决定 24 的 net-gateway 部分、蜜罐和可探测性自检移到不编号的"以后"，决定 6 的每沙箱 canary 和隔离自检留在
新 M1，`os_user` 已完成；M2 的决定 11、12 移到新 M1，决定 13–15、18–23 的恢复与接管移到新 M3，门槛里"停掉
net-gateway"一项随网络能力移到以后；M5 不变，Open question 12 随网络能力移到以后。沙箱改为 `--network none`
（轨迹分析优先 spec 决定 2），决定 24 的每沙箱网络留给网络能力。

- **M0**
  - 决定 1、2、5 的沙箱拓扑与加载校验；docker 驱动能让多个 agent 在同一容器里执行工具。
  - 决定 3 的文件 diff 与进程快照。
  - 决定 7–10、12：锁定 `inspect_ai` 版本；`runs.events` 存 Inspect `Event`；run 结束导出 `.eval` 到 RustFS（S3 API）；
    读回测试（`read_eval_log`）进 `mise run check`。
  - 决定 13、16：Postgres（`control` / `runs` 两个 schema，Alembic 迁移，测试用 testcontainers）；每次进上下文前提交的
    写前规则。表结构按可恢复设计，恢复逻辑在 M2。
  - 门槛：`.eval` 能被 `inspect view` 打开；共享沙箱里两个 agent 的文件写入能正确归属。
- **M1**
  - 决定 24：每沙箱网络与 net-gateway 透明网关。Open question 11 的验证已完成（2026-10-01）。
  - 决定 6：每沙箱 canary、按拓扑生成的隔离自检；`os_user`（Open question 7）。
  - 门槛：隔离的两个沙箱之间探针全部失败；探针直接连任意 IP 时事件流里出现对应的 `net.*` 事件，连宿主和平台服务全部失败。
  - （2026-10-02）决定 24 的 net-gateway 部分延后，理由和影响范围见
    [轨迹分析优先 spec](../2026-10-02-trajectory-first/README.md)。决定 24 本身不变，设计以 [docs/services/net-gateway.md](../../docs/services/net-gateway.md) 为准。
    停下时的进度：
    - 已合并（PR #4）：sandboxd 的每沙箱网络（`inhibit_ipv4`、`/28` 子网池）、`resolv.conf` 和 docker DNS 指向网关地址；
      守护测试 `TestLiveSandboxRoutesOnlyToItsGatewayWhichNobodyHoldsYet`。
    - 已写、未接入：`go/internal/netgw` 的 `Policy`（首条命中，
      `allow` / `deny` / `log_and_deny`）、`Config`（sandboxd 挂载的只读 JSON）、`classifyTCP`（SNI 与 HTTP 请求行）；
      `proto/swarmeval/netgw/v1/netevents.proto` 和生成的 Go、Python stub。`go.mod` 里预先加了
      `codeberg.org/miekg/dns`（DNS 用）。
    - 没写的，大致按这个顺序：net-gateway 二进制和容器镜像；启动时把网关地址加到各沙箱网络的网卡、nftables TPROXY 抓包
      （`sigs.k8s.io/knftables`）；TCP / UDP 透明接收与转发；DNS 服务（合成地址池、NXDOMAIN 延迟）；TLS 拦截（每 run
      CA、按 SNI 签叶子证书）；`NetEventsService` 服务端与"ack 之后才放行"；pcap 分块上传；`route` / `delay` / `rewrite` /
      `read_only` 动作。平台侧：sandboxd `CreateRun` 启动 net-gateway 和蜜罐网络；worker 生成 CA 和每 run 证书、编译
      `env.yaml` 的 `network:` 成 `Config`、拨 `NetEventsService` 并提交 `net.*` 事件；case 格式接受 `network`、`services`。
    - 开工前要定：Open question 2（`net.*` 组提交窗口）、13（无 worker 时等待还是拒绝）。
- **M2**
  - 决定 11：`Score` / metrics / reducer；决定 12：Parquet 导出与 run 级汇总表。
  - 决定 13–15、18–22：从 `agent_state` 恢复（与 fork 共用查询）、租约、写入 fencing、按标签认领容器、恢复保真度与 case
    下限、半截步骤处理、沙箱重启后的 `fs_preserved` 路径。
  - 决定 23：服务健康检测、暂停与冻结沙箱、恢复后的 `CheckpointEvent`。
  - 门槛：跑到一半杀掉 worker，重启后以 `exact` 保真度跑完，事件序列无缺口；run 中途在没有在途请求时停掉 net-gateway 再
    拉起，agent 没有收到任何连接错误，事件里记有这次暂停。
- **M5**：决定 17 的跨节点接管；Open questions 10、12 的节点操作与 Pod 出口。
