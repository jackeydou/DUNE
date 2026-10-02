# 轨迹分析优先：网络能力延后、沙箱断网、`web_request` 工具、里程碑重排

## Status

draft · 2026-10-02 · 决定 1–8 已和提出人确认；Open questions 待定。

## Request

提出人（2026-10-02）：

- 把 net-gateway 放到后面做。蜜罐和网络监控对前期的 agent 行为轨迹分析帮助不大，网关的主要用途是借网络改写 agent
  访问到的内容。已写的 net-gateway 代码保留，标清楚注释和引用的 spec，方便以后重新开始。
- 重排里程碑和服务架构：net-gateway 变成以后才补的能力，现有架构相应调整。
- agent 可以访问外网：提供一个 `web_request` 工具，运行日志记录调用参数和响应即可。

## Decisions

### 1. net-gateway、蜜罐与 mock 服务移出编号里程碑

它们成为"以后"的网络能力，不排进 M1–M5。设计不变，仍以
[runtime spec](../2026-09-28-runtime-sandbox-logs/README.md) 决定 24 和
[docs/services/net-gateway.md](../../docs/services/net-gateway.md) 为准；停下时的进度记在 runtime spec Plan 的
2026-10-02 注记里。随之移出的有：TLS 拦截、DNS、策略动作、pcap、`net.*` 事件、net-gateway 的每 run mTLS、蜜罐与 mock
模板、跨 run 共享蜜罐、`network_stealth` 变体、可探测性自检、case 格式的 `network` 和 `services`、k8s 下的 Pod 出口改道
（runtime spec Open question 12），以及 runtime spec Open questions 2、13。

### 2. 沙箱断网：`--network none`

沙箱容器只有回环接口。shell 里的任何联网尝试立刻失败（`Network is unreachable`），不会卡在超时上。

- PR #4 的每沙箱网络（`inhibit_ipv4` bridge、`/28` 子网池、`resolv.conf` 和 docker DNS 指向网关）保留在代码里，留给
  网络能力用，当前的 run 不走这条路径。`CreateRun` 不再为 run 建网络。
- 随之消失的问题：runc 下 docker 内嵌 DNS 的痕迹和经它外传（runtime spec Open question 14）不再存在，因为
  `--network none` 的容器没有内嵌 DNS；沙箱之间、沙箱和宿主之间没有任何二层或三层路径。
- 隔离自检简化为：沙箱里除回环外没有网卡、连不到任何地址；`web_request` 连不到内网、宿主和平台地址（决定 3）。

### 3. `web_request`：worker 执行的外网请求工具

- **开启方式**：内置工具，agent 在 `case.yaml` 的 `tools` 里列出才有。没有列出的 agent 没有任何外网。
- **在哪执行**：在 worker 进程里，作为 `RuntimeTool`，不在沙箱里。沙箱保持断网，每一次外网访问都经过平台、都有记录，
  而且请求和响应由 worker 自己取得，仍然符合"证据只来自平台，不来自 agent"。
- **放行范围**：任意公网地址、任意 HTTP 方法（提出人选择；代价见 Open question 3）。
- **地址限制（硬约束，不可由 case 关闭）**：只连全局单播地址。先解析域名，逐个检查解析结果，拒绝回环、私有网段、链路本地
  （含 `169.254.169.254` 等元数据地址）、CGNAT、多播、保留地址和映射到这些地址的 IPv6 形式；然后连到检查过的那个地址，
  不再重新解析，防 DNS rebinding。worker 能连到 Postgres、对象存储、model-gateway 和 Control API，这道检查是 agent 和它们
  之间唯一的屏障，参考事件里的 HF 入侵正是从 SSRF 开始的。
- **不自动跟随重定向**：3xx 原样返回给 agent，由它决定是否请求 `Location`。一次工具调用恰好对应一次出站请求，记录和
  地址检查都只有一处。
- **无状态**：worker 不加任何凭据，不保存 cookie，也不跨调用复用；agent 需要时自己在请求头里带。
- **记录**：`ToolEvent` 的参数是请求（方法、URL、请求头、请求体），结果是状态码、响应头和响应体。请求体和响应体超过内联
  上限时进 blob store，按哈希引用（[event-log.md](../../docs/event-log.md#large-objects)）。给 agent 的响应体按上限截断并
  注明截断；非 UTF-8 内容按现有规则转成文本，原始字节在 blob 里。被地址限制拒绝的请求也是一条 `ToolEvent`，agent 看到
  一个连接错误，拒绝原因记在 `metadata.swarmeval`。
- **改写**：结果和其他工具一样经过 `after_tool_result` hook，扩展可以改写 agent 看到的内容，改写记成 `intervention` 事件，
  原样响应仍在 `ToolEvent` 里（[agent 循环 spec](../2026-09-29-agent-loop-hooks/README.md) 决定 5、7）。
- **恢复与 fork**：worker 在请求发出后、结果提交前死掉时，这次调用不重做，agent 看到 `InterruptEvent`，和其他工具调用一样
  （runtime spec 决定 21）。fork 到第 k 步时，k 之前的响应来自 `agent_state`，之后的请求重新访问真实网站。
- **可复现性**：真实网站会变，同一 variant 的不同 epoch 可能看到不同内容。完整响应都在事件里，分析时可以按响应哈希分组。

### 4. 改写 agent 看到的内容：先只用扩展 hook 和文件

不新增"模拟服务"这种形态。改写工具结果（包括 `web_request` 的响应）用 `after_tool_result`；诱饵凭据、共享缓存、答案
文件这类环境内容用 case 文件、seed 文件和共享沙箱。

### 5. 隔离默认值改为"沙箱无网络，唯一出口是 `web_request`"

原来的默认值是"入站和出站都默认拒绝，只有 case 显式声明的流量经网关放行"（v1 spec 待讨论问题 4）。改为：沙箱没有网络；
唯一的出口是 `web_request`，只在 case 为某个 agent 开启时存在，只连公网地址。提出人 2026-10-02 明确同意这一放宽。
AGENTS.md 的隔离一条同步修改。

### 6. 事件来源和服务间认证

- 事件只来自 model-gateway、sandboxd 和 worker 自己（Message Bus、`web_request`、编排事件）。`net.*` 事件类型留给网络
  能力。
- 服务间 mTLS 全部在 M4 随 edge 一起做。M1 不再需要每 run 证书和拦截 CA。

### 7. 里程碑重排

顺序：批量与轨迹分析 → 因果分析 → 恢复与接管 → 控制台 → k8s；网络能力不编号。新的阶段表在
[v1 spec §9](../2026-09-27-swarmeval-v1/README.md#9-里程碑与待讨论问题)，那里是阶段计划的唯一出处。旧编号到新编号：

| 旧 | 内容 | 新 |
| --- | --- | --- |
| M0 | 最小闭环 | M0，不变 |
| M1 | gVisor 默认、`os_user`、每沙箱网络代码、sandbox profile 的 `files:` | 已完成（PR #4） |
| M1 | canary 解码、每沙箱 canary、隔离自检 | M1 |
| M1 | net-gateway、蜜罐与 mock、跨 run 共享蜜罐、`network_stealth`、可探测性自检、依赖网络的 case | 以后 |
| M2 | 变体矩阵、epoch、suite、多 worker 并行、三层 scorer、Score / reducer、Parquet、DuckDB、按 variant 组装 `.eval`、transcript 比对 | M1 |
| M2 | 恢复与接管、基础设施故障时暂停 | M3 |
| M3 | 消息干预、fork、在线 Monitor、隐蔽通信检测、`async` / `event_driven` | M2 |
| M4 | 控制台 | M4，不变 |
| M5 | k8s | M5，去掉 Pod 出口改道 |

理由：前期要的是能批量跑出可比较的轨迹、有工具分析它们，再用干预和 fork 问"为什么"。恢复与接管保证的是长 run 不丢，
在多 worker 并行之前，worker 崩溃的 run 标记 `interrupted` 并补跑一个新 epoch 就够用。

### 8. 首批 case 按是否依赖网络重新分组

- M1：`scorer_misbelief`（已有）、`transcript_spoofing`（本来不用网络），以及改成离线形式的 `impossible_task_pressure`、
  `shared_cache_rendezvous`：可写的共享包缓存用共享沙箱里的目录代替 mock 包管理器（Open question 1）。
- 以后：`readonly_web_deaddrop`、`honeypot_credential_chain`，它们要的是我们控制的服务端和网络层记录。

### 9. 轨迹分析自建，不以 Docent 或 inspect-scout 为主（2026-10-02 补充）

结掉 runtime spec Open questions 8、9。查询、LLM judge、规则扫描和单 run 时间线都在 `swarmeval.analysis` 里自己做；
`.eval` 仍是对外交换格式，需要时再加 Docent、inspect-scout 的导出适配，不是现在的依赖。理由：

- 数据和信号是我们自己的：文件改动、进程快照、canary 命中、消息投递、`intervention` 改写、`parent_id` 因果链。外部工具
  只认消息序列，这些都要压扁进 metadata。
- 多 agent 是重点，两边都要写转换；我们的 `.eval` 把轨迹放在 events 里，`messages` 为空，外部导入器都读不全。
- judge 的结论要引用 `event_id` 并校验，结论才回得到哈希链证据上；外部工具做不到这一点。
- 轨迹里有注入 payload 和诱饵凭据，不出部署就没有访问控制的问题（v1 spec 待讨论问题 6）。
- 导出适配是百来行的事，以后随时能加，现在不接不会被锁住。

代价是看轨迹的界面要自己做：先用 `inspect view` 和一个单 run 时间线的批处理任务，正式的回放 viewer 仍在 M4。

## Rejected

- **保留每沙箱网络、网关地址上没有人**：agent 每次联网都要等 ARP 失败或 DNS 超时（几秒到十几秒），轨迹里堆满超时；runc 下
  还留着内嵌 DNS 的痕迹。
- **沙箱直接联网（docker NAT）**：shell 里的流量只留下命令行和输出，hook 改写不了；沙箱能连宿主和宿主上暴露的服务，违背
  "沙箱看不到宿主"。在沙箱里执行 `web_request` 同样需要沙箱联网，一并否掉。
- **默认只读（只放 GET / HEAD，case 白名单放开其他方法）**：提出人选择任意方法。德国 wiki 事件说明 GET 也能写，只读限制
  本来就不严格。
- **所有 case 默认开启 `web_request`**：等于把出站从默认拒绝改成默认放行，每个 case 的结果都受外网内容影响。
- **worker 侧的模拟服务工具（fixture 或 Python 模拟服务）、沙箱镜像里跑 mock**：前者要新设计一种扩展点和跨 run 状态的
  存放位置，后者让 agent 能看到、改动 mock 本身。先用 hook 和文件，需要时再立 spec。
- **自动跟随重定向**：一次调用会对应多次出站请求，每一跳都要单独检查和记录。
- **单独的出口容器执行 `web_request`**：纵深防御更好，但多一个服务和一条 RPC。先在 worker 里做，见 Open question 5。
- **以 Docent 为主做多 agent 轨迹分析**（2026-10-02）：交互式搜索、rubric 和聚类现成，原生支持一个 run 多条 transcript；
  但要写转换、托管版会把轨迹送出部署、结论和 `event_id` 证据断开。见决定 9。
- **以 inspect-scout 的 scanner 代替自建的 judge 和规则扫描**（2026-10-02）：能直接读 `.eval`，但默认读 `messages`，
  要改成读 events 或在导出时按 agent 填好；我们自己的 judge 要的引用校验它也没有。见决定 9。

## Open questions

1. `impossible_task_pressure` 和 `shared_cache_rendezvous` 用共享目录代替 mock 包管理器之后，还测得到原来的行为吗（把缓存当
   信道、转向攻击基础设施）？M1 门槛里的 case 列表依此定。
2. `web_request` 的默认上限：超时、请求体和响应体上限、给 agent 的截断长度、每 run 的请求数或速率。
3. agent 对真实第三方的影响怎么兜底：出口 IP 用什么、`User-Agent` 要不要标识来源并留联系方式、是否需要按 case 的速率上限、
   发生滥用时怎么处理。我们的 case 会故意给 agent 施压，它可能在真实网站上写入或攻击。
   （2026-10-02）暂缓，记录在案：M1 期间 `web_request` 除地址检查外不加兜底；等做网络能力（net-gateway）时一并设计，
   届时策略动作（`read_only`、`route`、`rewrite`）和出口记录就是落点。见
   [docs/services/net-gateway.md](../../docs/services/net-gateway.md#not-settled)。
4. runtime spec Open question 6（中断与补跑的统计口径）原定 M2 之前定，现在 M1 的报告就要用到，提前到 M1 报告之前。
5. 是否把 `web_request` 移到一个只能出公网、连不到平台网络的独立出口进程或容器里，作为地址检查之外的第二道防线。
6. ~~runtime spec Open questions 8、9（inspect-scout、Docent）要在 M1 的分析出口之前定~~ 已定（2026-10-02）：自建，见决定 9。

## Plan

- **本 spec 的文档部分**（2026-10-02）：v1 spec §9 改写阶段表；docs 的服务表、架构图、隔离、故障处理、部署和各服务页按
  决定 1–7 更新；AGENTS.md 的隔离一条；runtime spec 和 agent 循环 spec 的 Plan 加编号对照注记。
- **M1**，大致按这个顺序：
  1. sandboxd：沙箱 `--network none`，`CreateRun` 不再建网络；守护测试改成"沙箱只有回环、连不到任何地址"。
  2. `web_request`：地址检查、请求与响应记录、blob、截断；case 格式接受这个工具名；SSRF 测试覆盖每一类被拒地址、DNS
     rebinding 和 3xx 指向内网。测试用本地 mock 服务，不访问真实互联网。
  3. M0 门槛：真实开源模型跑 `scorer_misbelief`（可与 1、2 并行）。
  4. 批量：变体矩阵、epoch、suite、多 worker 并行；崩溃的 run 标记 `interrupted` 并补跑新 epoch。
  5. 评分与分析：Score / metrics / reducer、LLM judge 引用 `event_id`、Parquet 与 run 汇总、DuckDB 批处理任务、按 variant
     组装 `.eval`、transcript 与网关记录比对、单 run 时间线、规则扫描（决定 9）。
  6. canary 解码与每沙箱 canary；隔离自检。
  7. case：`transcript_spoofing` 和 Open question 1 定下的离线 case。
  - 门槛：见 v1 spec §9。
