# M4 控制台：edge、case 库、analysis 服务、Web 与 CLI、单机部署

## Status

draft · 2026-10-03 · 决定 3、5、6 和实现顺序由提出人选定（2026-10-03）；其余决定是起草人的建议，Open questions
未回答前，实现按各条注明的做法。

## Request

提出人（2026-10-03）先问"M4 阶段有哪些不依赖 M2、M3 的事情"，得到的回答是 M4 的门槛不需要 M3 的任何能力，
随后要求"开始做M4"。

M4 的目标和门槛以 [v1 spec §9](../2026-09-27-swarmeval-v1/README.md#9-里程碑与待讨论问题) 为准：

- 目标：对外可用的单机平台。
- 内容：edge（认证、租户、控制台后端）；服务间 mTLS；Control API 补全 case CRUD；analysis 服务化（事后规则扫描、
  交互式 judge）；`console/` Web 前端与回放 viewer；Go CLI；单机自部署（物理机或 VM，docker compose）。
- 门槛：能在 Web 上创建 case、触发一次评估运行、看到结果和回放；CLI 能完成同样的操作。

这推翻了 v1 spec 待讨论问题 7（"控制台等 M0–M3 做完再做，不并行推进"）：M3（恢复与接管，#14）还没合并。
推翻的范围只是开工时间，M4 不依赖 M3。M3 合并后要补的显示项见决定 12。

已定、本 spec 不重复的：edge 是唯一对外入口，任何调用都要凭证（v1 spec §3）；认证后看得到所有 workspace（待讨论
问题 8）；CLI 是 edge 的薄客户端，不 import `swarmeval`（v1 spec §3、§8）；analysis 自建，不以 Docent、
inspect-scout 为主（轨迹分析优先 spec 决定 9）；`allow_case_code` 默认关闭（M2 spec 决定 4）。

## Decisions

### 一、对外 API

#### 1. 对外 API 是单独的 proto 包 `swarmeval.api.v1`，edge 负责映射

- `proto/swarmeval/api/v1/` 定义对外服务：`AuthService`（登录、登出、当前用户、API token）、`UserService`
  （管理员管理用户）、`RunService`、`CaseService`、`AnalysisService`。edge 用 connect-go 提供，浏览器走 Connect
  协议的 JSON，CLI 走 gRPC。
- 内部的 `swarmeval.control.v1.ControlService` 和新的 `swarmeval.analysis.v1.AnalysisService` 不直接对外。edge
  把对外请求翻译成内部调用，再把结果翻译回去。
- `buf breaking` 只对 `swarmeval/api/v1` 生效，并加进 `mise run check`。内部 proto 跟着服务一起改。

理由：内部消息带有 `owner_id`、租约、bundle 字节这类实现细节，直接对外暴露的话，每次内部重构都会变成对外的破坏性
变更。对外 API 是和 `case.yaml` 同等级的用户契约，要单独管理版本。代价是 edge 里多一层映射，每个 RPC 十几行。

#### 2. edge 把调用者身份传给内部服务，只记在控制面的行上

- `SubmitRuns`、`ForkRun`、`CancelRun`、`ResumeRun` 以及 case 的写操作，在内部请求里带 `actor`（用户名）。控制面
  把它写在 `control.run_specs.submitted_by`（提交和 fork；补跑沿用被补跑的那个 run 的值）、`control.runs` 的
  `cancelled_by` 和 `resumed_by`（最后一次恢复），以及 `control.case_revisions.actor` 上。Control API 的 `Run`
  消息和 case 修订会返回这些值。
- 事件流里不记 actor，事件 schema 不变（Open question 1）。
- 内部服务信任 edge 传来的 `actor`：只有持 edge 证书的调用方能连上它们（决定 10）。

### 二、认证

#### 3. 内置账号、浏览器会话、个人 API token

提出人选定（2026-10-03）。

- 用户存在 `tenant.users`：用户名、argon2id 密码哈希（PHC 字符串，参数取 RFC 9106 第二推荐组：m=64 MiB、t=3、
  p=4）、角色（`admin` 或 `member`）、是否停用、创建时间。
- 浏览器：`AuthService.Login` 校验密码后创建会话，响应里下发 cookie。cookie 只有 HttpOnly、SameSite=Strict，带
  `Path=/`；配置的公开地址是 https 时再加 `Secure` 和 `__Host-` 前缀。会话存在 `tenant.sessions`，库里只存随机值
  （32 字节）的 sha256。会话闲置 24 小时或创建满 7 天后失效（Open question 2）。
- CLI：用户在控制台或用 `swarm login`（输入用户名和密码，换一个 token）生成个人 API token。token 形如
  `swm_` 加 32 字节随机值的 base32，库里只存 sha256，明文只在创建时显示一次。token 有名字、可选的过期时间和最后
  使用时间，可以随时吊销。请求头为 `Authorization: Bearer <token>`。
- 每个请求在 edge 的拦截器里认证，认证失败返回 `UNAUTHENTICATED`，不区分"用户不存在"和"密码错"。除了
  `AuthService.Login`，没有免认证的 RPC。静态页面不需要认证，页面里的数据请求需要。
- CSRF：带 cookie 的请求必须有 `Origin` 头，并且等于配置的公开地址；Connect 的 JSON 请求要求
  `Content-Type: application/json`，HTML 表单发不出这种请求。
- 登录限速：同一用户名或同一来源地址，连续 5 次失败后锁 1 分钟，此后每次失败锁定时间翻倍，上限 15 分钟。计数存在
  进程内存里，edge 重启后清零。
- 第一个管理员用 `edge user create --admin <name>` 创建，密码从标准输入读入。没有默认账号，也没有默认密码。
- `member` 能做除管理用户以外的所有事；`admin` 另外能建用户、停用用户、重置密码。不区分 workspace（待讨论问题 8，
  决定 5）。

#### 4. edge 自己终止 TLS

- edge 配了证书和私钥时监听 https。没配时只允许监听回环地址，用于开发和放在反向代理后面的部署。
- `--public-url` 是浏览器访问 edge 用的地址，`Origin` 检查和 cookie 的 `Secure` 都按它来。

#### 5. M4 不建 tenant、workspace 表

提出人选定 M4 不做 workspace 访问隔离（2026-10-03）。`tenant` schema 在 M4 只有 `users`、`sessions`、
`api_tokens`。workspace 仍然只是 case、run、事件上的一个字段。控制台需要的 workspace 列表从 case 库里去重得到。
等做访问隔离时，再加 workspace 和成员关系的表。

理由：现在建这些表没有任何读取方，只会让以后的设计被一个没用过的结构绑住。

### 三、case 库

#### 6. 版本化的 case 库，修订不可变

提出人选定（2026-10-03）。

- `control.cases`：`workspace`、`case_id`（即 `case.yaml` 的 `id`），`(workspace, case_id)` 唯一；另有
  `archived_at`。
- `control.case_revisions`：所属 case、修订号（从 1 开始递增）、bundle 的 sha256、`actor`、创建时间、可选的说明。
  一行写入后不再修改。bundle 照旧按 hash 存在对象存储的 `cases/sha256/<hex>.tar`。
- 写入方式有两种：
  - **推送整个目录**（CLI、`SubmitRuns`）：bundle 校验通过后，如果和这个 case 的最新修订 hash 相同，就沿用最新
    修订，否则新建一个修订。`case.yaml` 的 `workspace` 和 `id` 决定它属于哪个 case，不存在就创建。
  - **编辑文件**（控制台）：`UpdateCaseFiles(case, base_revision, 改动)`，改动是"路径 → 新内容"或"删除"。
    服务端在 `base_revision` 的 bundle 上应用改动，生成新 tar，校验通过才存成新修订。`base_revision` 不是最新
    修订时返回 `ABORTED`，并带上最新修订号，控制台据此提示"已被别人改过"。
- 校验用的是 `SubmitRuns` 现在用的同一个加载器。加载失败返回 `INVALID_ARGUMENT` 和加载器的报错，什么也不存。
  case 带 `case:` 代码而部署没开 `--allow-case-code` 时，仍和现在一样直接拒绝。
- `SubmitRuns` 增加一种入参：`(case, revision)`，和现在的 bundle 二选一。每个 run 都引用一个修订
  （`control.runs` 加 `case_revision_id`）。
- 删除 case 只是归档：归档后列表默认不显示，也不能再提交新 run。修订和 bundle 永远不删，因为 run 引用它们，
  它们是证据的一部分（Open question 3）。
- 迁移时，按现有 run 的 `workspace`、`case_id`、`case_sha256`，以每个 hash 第一次被使用的时间为序回填 case 和修订，
  这样老 run 也挂到 case 库上。
- 修订之间的 diff 由控制台在浏览器里算（两个修订的文件列表和内容都能取到），服务端不做。

#### 7. 控制台里能编辑哪些文件

bundle 里所有 UTF-8 文本文件都能编辑、新建、删除，包括 `case.yaml`、`env.yaml`、提示词和 `case:` 代码；二进制
文件只能整个上传替换或删除。单个文件上限 1 MiB，整个 bundle 上限 64 MiB（沿用 Control API 的消息上限）。
`case:` 代码可以编辑，能不能运行仍由部署开关决定（Open question 4）。

### 四、analysis 服务

#### 8. analysis 加一个 gRPC 服务入口，包装现有的批处理任务

- 新入口 `swarmeval-analysis`，服务 `swarmeval.analysis.v1.AnalysisService`，只有 edge 能调用。
- 每个 RPC 复用现有任务的代码，不另写一套：

  | RPC | 对应 | 说明 |
  | --- | --- | --- |
  | `Query` | `swarm query` | 只读 SQL，结果分块流式返回。默认最多 10,000 行、30 秒，超出就截断或取消，并告知调用方 |
  | `SearchToolCalls` | `Query` 的结构化版本 | 按工具名、agent、run、时间范围、tag 过滤，控制台的过滤器用它 |
  | `StartRuleScan`、`GetJob` | `scan` | 规则扫描是后台任务，返回 job id；任务状态存在 `analysis.jobs` |
  | `Judge` | `judge` | 一次交互式 judge 请求，同步返回结论和引用的事件 |
  | `Report` | `report` | 按 submission 或 suite 出触发率和区间，可带 `compare` |
  | `GetTrace` | `trace` | 一个事件的因果链 |
  | `DownloadExport` | — | 流式返回一个 run 的 `sample.eval` 或 `events.parquet`，CLI 的 `swarm export` 用它 |

- DuckDB 连接在注册完 Parquet 视图之后关闭外部访问并锁定配置（`enable_external_access=false`、
  `lock_configuration=true`），用户的 SQL 读不到别的文件，也改不了这个设置。
- `DownloadExport` 放在 analysis 而不放在 edge：analysis 本来就读对象存储，edge 不需要对象存储的凭据。

### 五、控制台与回放

#### 9. `console/` 是单页应用，构建产物嵌进 edge

- 技术栈：TypeScript、React、Vite；pnpm；用 buf 生成的 Connect-ES 客户端（`@connectrpc/connect-web`，
  `@bufbuild/protobuf` v2）；TanStack Router 和 TanStack Query；YAML 编辑器用 CodeMirror 6；样式用
  Tailwind CSS 加 shadcn/ui。具体版本在开工时核对仓库状态，原则见 AGENTS.md。
- 构建产物在 edge 构建时用 `embed.FS` 嵌入，edge 一个二进制同时提供页面和 API。开发时 Vite dev server 把 API
  代理到本机的 edge。
- 页面：

  | 页面 | 内容 |
  | --- | --- |
  | 登录 | 用户名、密码 |
  | Runs | 按 workspace、case、suite、状态过滤的列表；每个 submission 的触发率（`Report`） |
  | Run 详情 | 状态、变体取值、隔离等级、分数（事件流里的 `ScoreEvent`）；取消、恢复；回放 |
  | 回放 | 每个 agent 一条泳道，另有一条放不属于任何 agent 的事件；运行中的 run 实时追加；点开一个事件看全文和因果链（沿 `parent_id`，跨 fork 进入原 run）；按事件类型和 tag 过滤；两个 run 并排比较；从某个事件 fork |
  | Cases | 列表、修订历史、修订之间的 diff、编辑文件、按变体覆盖和 epoch 提交运行 |
  | Analysis | SQL 查询、工具调用过滤、规则扫描、交互式 judge |
  | 账号 | 修改密码、管理 API token |
  | 用户（admin） | 建用户、停用、重置密码 |

- 回放的数据来自 `RunService.StreamEvents`。它读 `runs` schema，运行中和已结束的 run 用同一条路径，控制台不读
  Parquet。
- 事件内容（模型输出、工具结果、注入 payload）一律当文本渲染，不当 HTML 渲染；Markdown 只在明确标记的字段上
  渲染，并且关闭原始 HTML。轨迹里本来就有故意构造的恶意内容。

### 六、服务间 mTLS

#### 10. 自签 CA，每个服务一张证书，按证书身份放行

- `go/cmd/swarm-certs` 生成一个 CA，再为每个服务签一张证书（同时用作服务端和客户端证书）。证书的 SAN 里带一个
  URI `spiffe://swarmeval/<service>`，作为服务身份。证书有效期 1 年，重新运行即可轮换，轮换后重启服务。用
  Go 标准库的 `crypto/x509` 实现，不引入别的 PKI 组件。
- 每个服务只接受下表里调用方的证书：

  | 服务 | 接受的调用方 |
  | --- | --- |
  | orchestrator 控制面（`ControlService`） | edge、`operator` |
  | analysis | edge |
  | model-gateway（HTTP 和 `RecorderService`） | worker、analysis |
  | sandboxd | worker |

  `operator` 证书给运维工具使用，比如 `swarmeval.control.suite` 和 grpcurl。
- 服务配了证书就强制 mTLS。没配证书时只允许监听回环地址，用于开发和测试，和决定 4 一样。compose 部署始终配证书。
- Python 端：grpcio 用 `ssl_server_credentials(require_client_auth=True)`，uvicorn 用 `ssl_cert_reqs=CERT_REQUIRED`，
  httpx2 带客户端证书；身份检查放在 gRPC 拦截器和 ASGI 中间件里。

### 七、CLI

#### 11. `swarm` 的命令

Go + cobra，单个静态二进制。endpoint 和 token 读 `~/.config/swarm/config.yaml`，环境变量 `SWARM_ENDPOINT`、
`SWARM_TOKEN` 覆盖文件里的值。

| 命令 | 做什么 |
| --- | --- |
| `swarm login` | 输入用户名和密码，换一个 API token 写进配置文件；也可以 `--token` 直接写入 |
| `swarm run <case 目录 \| suite 文件>` | 推送 case（决定 6），再按 `-V axis=values`、`--epochs` 提交；`--follow` 跟随事件，直到所有 run 结束 |
| `swarm runs [list \| get \| cancel \| resume]` | 管理 run |
| `swarm events <run> [--follow]` | 逐行打印事件，格式和 judge 读到的一致 |
| `swarm view <run>` | 打印并打开控制台里这个 run 的回放页面 |
| `swarm replay <run> --fork-at <event> --edit <file>` | `ForkRun` |
| `swarm query "<sql>"` | `Query`，表格或 CSV 输出 |
| `swarm report --submission ID [--compare AXIS=A,B]` | `Report` |
| `swarm export <run> [--format eval \| parquet]` | `DownloadExport` |
| `swarm case [list \| push \| pull \| revisions]` | case 库 |
| `swarm token [create \| list \| revoke]`、`swarm user …`（admin） | 账号 |

不在 M4：`swarm env up`（只起环境、手动调蜜罐，依赖网络能力）；`--format otel | docent`（导出适配是"以后可加"，
轨迹分析优先 spec 决定 9）；没有服务可连时自动拉起本地实例（Open question 5）。

（2026-10-03）实现时的四处改动：

- CLI 走 Connect 协议，不走 gRPC（决定 1 原写"CLI 走 gRPC"）。Connect 协议在 HTTP/1.1 上也能用，过反向代理不需要
  代理支持 gRPC，CLI 对网络的要求和浏览器一样。edge 仍然接受 gRPC，比如 grpcurl。
- `swarm events` 总是跟随到 run 结束，不另设 `--follow`：`StreamEvents` 没有"只取当前已有事件"的模式，Ctrl-C 只停止
  跟随。
- 提交 suite 走新的 `SubmitSuite`（Control API 和对外 API 都有）：CLI 只读 suite 文件里的 `cases[].path` 来打包，
  其余格式由控制面用同一个加载器校验，suite 格式不在 Go 里再实现一遍。所有 run 在一个事务里入队，坏 case 什么都
  不提交。`python -m swarmeval.control.suite submit` 也改走它。
- 事件的单行文本由控制面渲染（`StreamEvents` 的 `line`，即 judge 读的那一行），渲染规则只在
  `swarmeval.events.render` 一处，CLI 只负责打印。`render` 从 `swarmeval.analysis` 挪到 `swarmeval.events`，控制面
  不必依赖 analysis。

### 八、与 M3 的衔接

#### 12. M3 合并后要补的显示项

M4 不等 M3。M3 合并后：Run 详情显示恢复保真度和接管次数；回放渲染恢复用的 `CheckpointEvent` 和基础设施暂停；
compose 的 worker 重启策略按接管语义调整。这些放在 M4 Plan 的最后一步，M3 还没合并就跳过。

### 九、单机部署

#### 13. `deploy/compose/`

- 镜像：Python 一个镜像，`swarmeval-control`、`swarmeval-worker`、`swarmeval-model-gateway`、
  `swarmeval-analysis` 是不同的入口；Go 一个镜像，包含 `edge`、`sandboxd`、`swarm-certs`。版本一致，一起发布
  （architecture.md 的部署一节）。
- 服务：`certs`（一次性，生成证书到一个卷）、`postgres`、`rustfs`、`control`、`worker`、`model-gateway`、
  `sandboxd`、`analysis`、`edge`。只有 edge 发布端口。
- 网络：平台服务在一个 internal 网络里；worker 另外接一个能出公网的网络，供 `web_request` 使用；其他服务都出不了
  公网。sandboxd 挂 docker socket，按 sandboxd README 的要求以相同路径挂载状态目录。
- 迁移：`control` 启动时跑 Alembic（现状不变）；edge 启动时用 goose 跑 `tenant` schema 的迁移（决定 14）。
- 模型后端不在 compose 里：model-gateway 的配置指向外部的 vLLM 或托管 API。
- 遵守共享服务器的规则：不重启 docker daemon，不动全局 iptables，只清理自己打了标签的资源。

#### 14. `tenant` schema 的迁移归 edge，用 goose

edge 用 `github.com/pressly/goose/v3` 跑 SQL 迁移文件，迁移文件嵌进二进制，版本表放在 `tenant` schema 里，和
Alembic 的版本表互不干扰。理由：每个服务只拥有自己的数据（architecture.md）。如果用 Alembic，`tenant` 表的定义
就会落在 Python 包里，edge 的镜像也会依赖 Python 镜像先跑完迁移。开工前核对 goose 的维护状态。

## Rejected

- **直接把内部 proto 用 Connect 暴露出去**：edge 最省事，但内部重构会变成对外的破坏性变更。见决定 1。
- **先做 OIDC**（提出人 2026-10-03 未选）：单机自部署要先有一个 IdP。以后需要时加在内置账号旁边。
- **无状态的 JWT 会话**：没法即时吊销；停用用户和吊销 token 都要求立刻生效。会话和 token 每次请求都查一次库，
  在这个规模下不算成本。
- **只在反向代理上做 TLS**：要求每个部署都配一个代理，cookie 的 `Secure` 也依赖代理的配置。edge 自己终止 TLS，
  代理变成可选。
- **git 仓库作为 case 库**（提出人 2026-10-03 未选）：部署多一个依赖，k8s 下还要共享仓库；修订历史用两张表就能
  得到。
- **只上传、不在线编辑**（提出人 2026-10-03 未选）：门槛要求"在 Web 上创建 case"，只能上传会让浏览器里的工作流
  断掉。
- **M4 建 tenant、workspace 表**：没有读取方。见决定 5。
- **`tenant` 迁移也走 Alembic**：见决定 14。
- **edge 直接读对象存储来下载导出**：edge 会多一份对象存储凭据，而 analysis 已经有了。见决定 8。
- **服务端渲染（Next.js 等）**：多一个 Node 运行时服务，只为了几个页面；单页应用嵌进 edge，部署时不多一个进程。

## Open questions

1. 取消、恢复、fork 由谁发起，只记在控制面的行上（决定 2），够不够？要进事件流的话，就是一次事件 schema 变更，
   需要升 `schema_version`。实现先按"只记在控制面的行上"。
2. 会话闲置 24 小时、最长 7 天，合适吗？实现先按这个。
3. 修订和 bundle 永远不删。要不要提供"删除没有任何 run 引用的修订"？实现先不提供。
4. 控制台允许编辑 `case:` 代码，运行仍受 `--allow-case-code` 控制（决定 7）。要不要在 edge 层面再加一个"禁止
   上传 case 代码"的开关？实现先不加。
5. CLI 在没有服务可连时自动拉起本地实例（v1 spec §3 提到的"可以"），放进 M4 吗？实现先不做。

## Plan

每一步一个 PR，各自满足 AGENTS.md 的"完成标准"：`mise run check` 通过、更新 CHANGELOG 和 docs、有测试。测试不调
真模型、不连真实互联网。

1. **spec 与对外 proto**：本 spec；`proto/swarmeval/api/v1` 的 `AuthService`、`UserService`、`RunService`；connect-go
   的代码生成；`buf breaking` 进 `mise run check`。`CaseService`、`AnalysisService` 随第 4、5 步加入，Connect-ES
   的生成随第 6 步。
2. **edge：认证与 run 转发**：`go/cmd/edge`、`go/internal/edge`；goose 迁移；用户、会话、token、登录限速；
   `edge user create`；`RunService` 转发到 `ControlService`；Control API 加 `actor`。
   退出条件：用测试替身的 `ControlService` 和 testcontainers 起的 Postgres 做集成测试，覆盖登录、token、
   未认证被拒、CSRF 被拒、提交、跟随事件、取消、恢复、fork。
   （2026-10-03）已实现：`edge serve`、`edge user create`；`tenant` 迁移（goose）；三个服务；Control API 的
   `actor` 和迁移 0009。除了上面的 Go 集成测试，另有一个 Python 端到端测试：构建 edge 二进制，接到真实的
   grpcio Control API 和 worker 上，用 Connect 的 JSON 协议提交 case、跑完、取回事件流，验证了 connect-go 与
   grpcio 之间的 gRPC 互通。实现时补上的细节：登录本身也检查 `Origin`（防止跨站种下会话）；`ChangePassword`
   和登录共用限速；对 Control API 的非调用方错误一律返回 `UNAVAILABLE`，原因只进日志；同时最多 4 个 argon2id
   计算。都写在 [docs/services/edge.md](../../docs/services/edge.md)。
3. **CLI**：`go/cmd/swarm`，run 相关命令和账号命令。
   退出条件：对着本地的 edge、control、worker 和录制好的模型后端，`swarm run cases/scorer_misbelief --follow`
   能跑完。
   （2026-10-03）已实现：决定 11 表中除 `view`、`query`、`report`、`export`、`case` 之外的命令（这几个随第 4、5、6
   步），以及上面注记的 `SubmitSuite` 和事件的 `line`。端到端测试用构建出的 `swarm` 和 `edge` 二进制，接真实的
   Control API 和 worker（模拟模型后端）：登录、`run --follow` 跑完并打印事件和状态表、`runs get --json`、
   `events`、提交 suite 并按标签列出、坏 suite 返回错误、登出后 token 失效。
4. **case 库**：Alembic 迁移（含回填）；Control API 的 case RPC；`SubmitRuns` 按修订提交；edge 的 `CaseService`；
   `swarm case`。
   退出条件：推送、编辑、并发冲突返回 `ABORTED`、归档、按修订提交都有测试；回填的迁移在一份带老 run 的数据库上
   测过。
5. **analysis 服务**：`swarmeval-analysis` 和决定 8 的 RPC；`analysis.jobs`；edge 转发；`swarm query`、
   `swarm report`、`swarm export`。
   退出条件：每个 RPC 有测试；`Query` 尝试读别的文件、修改配置都会失败。
6. **控制台**：`console/` 和决定 9 的页面，嵌进 edge。
   退出条件：Playwright 对着本地栈跑一遍门槛流程：登录、新建 case、编辑、提交、看分数、回放、fork。
7. **mTLS**：`swarm-certs`；各服务的 TLS 参数和身份检查。
   退出条件：每个服务拒绝不在白名单里的证书、拒绝没有证书的连接；没配证书时拒绝监听非回环地址。
8. **compose 与门槛**：`deploy/compose/`、镜像、部署文档。
   退出条件：在一台干净的 Linux 机器上 `docker compose up` 后，用录制的模型后端跑完 M4 门槛：Web 和 CLI 各走一遍
   "创建 case → 运行 → 看结果和回放"。
9. **M3 衔接**（M3 合并后）：决定 12。
