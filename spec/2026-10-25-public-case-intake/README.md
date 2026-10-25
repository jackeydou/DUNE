# 公开 case 征集站：只收 case，不跑 case

## Status

accepted · 2026-10-25 · 决定 1、2 由提出人选定；其余决定由起草人提出，提出人同日确认"都按推荐的来"，
原来的七个 Open questions 按起草人的建议回答，已写进决定 5、6、7、12 和 Plan。

## Request

提出人（2026-10-25）："我想把我们的 case 登记和收集网站发布到公网可访问，只负责收集 cases，完全不可以跑这个
case，作为一个对外发布公开征集安全合规 cases 的入口，我们自己的本地服务可以拉取线上数据筛选过滤之后，我们定期
来跑结果，把分析结果可以定期对外公布。"

讨论后（同日）提出人确认："单独开一个征集站，按照我们的 case 结构和展示规范来做一个新的站点和服务，部署在
Cloudflare 上，DB 就使用 Cloudflare 的 Hyperdrive 来连接 Neon PostgreSQL。"

现状：唯一对外入口是 edge（[docs/services/edge.md](../../docs/services/edge.md)）。它持有到 Control API 和
analysis 的 mTLS 证书，公开 API 里有 `SubmitRuns`、`ForkRun`、`Query`；没有自助注册，任何登录用户都看得到全部
workspace；存 case 时 control plane 用 worker 的 loader 加载它。case 的 `case:` 扩展在 worker 进程里以 worker
的权限运行（[orchestrator.md](../../docs/services/orchestrator.md#case-code)），`env.yaml` 的 profile 可以写任意
`image`（[case-format.md](../../docs/case-format.md#envyaml)）。这些对公网投稿者都不能开放。

## Decisions

### 一、边界

#### 1. 征集站是一套独立部署，和平台之间没有任何连接（提出人选定）

- 征集站（下称 intake）不持有平台的任何凭据：没有部署 CA 签的证书、没有模型 key、没有平台 Postgres 或对象存储的
  地址。Cloudflare 账号里不放任何指向内网的 binding、Tunnel 或 Workers VPC。
- 数据只沿一个方向流动：内网主动连 intake，拉取投稿、回写审核状态、上传公开结果。intake 从不连内网，也不向内网
  推送（没有 webhook）。
- intake 被攻破时，攻击者拿到的是投稿和投稿人账号，没有一条路通向 worker、sandboxd 或 model-gateway。

理由：提出人要的是"完全不可以跑"。把 edge 开到公网再关掉部分 RPC，跑不跑只隔着一份配置；独立部署让它在物理上
做不到。

#### 2. 跑在 Cloudflare 上：一个 Worker，Hyperdrive 连 Neon Postgres，R2 放公开结果（提出人选定）

- 一个 Worker 同时提供页面（Workers static assets，`not_found_handling: single-page-application`）和 API
  （`run_worker_first` 只覆盖 API 和 OAuth 路径）。开启 Smart Placement，让多查询的请求在 Neon 区域附近执行。
- 数据库：Neon Postgres，经 Hyperdrive。按 Cloudflare 的 Neon 指引用 Neon 的直连串（不是 pooled 串），驱动用
  `pg`（node-postgres，8.16.3 以上，需要 `nodejs_compat`），不用 Neon 的 serverless 驱动：池化由 Hyperdrive 做。
- **Hyperdrive 的查询缓存关闭。** 默认会把只读查询缓存 60 秒，投稿人保存后立刻读回会看到旧内容，审核状态也会
  滞后。这个站的读量不需要缓存。
- Neon 里给 Worker 一个只拥有 `intake` schema 的 role；迁移用另一个 role 直连 Neon（不经 Hyperdrive）。
- 投稿的文件存 Postgres，不放 R2：单个投稿最多 1 MiB（决定 6），和元数据在同一个事务里写入。R2 只放内网发布的
  公开结果（决定 12）。
- 人机验证用 Turnstile，在创建和修改投稿时校验，服务端调用 siteverify。登录不再加一道：GitHub 自己有人机
  验证。

### 二、代码与契约

#### 3. 新项目 `intake/`；页面是 console 的第二个构建入口

- `intake/`：TypeScript 的 Worker，有自己的 `package.json`、`wrangler.jsonc`、`migrations/`、README、CHANGELOG、
  BUGFIX，在 [repo-layout.md](../../docs/repo-layout.md) 的项目表里成为第四个项目。
- 页面放在 `console/src/intake/`，是 console 的第二个 Vite 入口，构建到 `intake/public/`，由 Worker 当静态资产
  发布。这和 console 构建进 `go/internal/edge/webui/static/` 是同一个做法。
- 复用 console 已有的 case 展示：`CaseFlow`、`CaseNodeDetail`、`FileEditor`、`CodeEditor`、`lib/caseGraph`、
  `lib/flowLayout`、`lib/files`、`components/ui/` 和 Claude 主题，这就是"按我们的 case 结构和展示规范"。
  `FileEditor` 现在依赖 `swarmeval.api.v1` 的 `CaseFile` 类型，改为只依赖它用到的字段，两边都能传。
- intake 入口不得 import Runs、Analysis、Users 页面和 `@/api`（edge 的客户端）。用一条 eslint
  `no-restricted-imports` 规则守住，这样公网包里不会带着平台 API 的形状。
- 内容渲染沿用 console 的规则：投稿内容一律作为文本节点，不渲染 HTML、不渲染 Markdown（投稿本身就是攻击
  payload）；CSP 只允许同源脚本和连接，禁止被嵌入。

#### 4. 契约是 `proto/swarmeval/intake/v1`，Worker 用 Connect-ES 提供

- 新 proto 包和 `swarmeval.api.v1` 分开，各自演进；`buf breaking` 加上这个包。
- buf 生成 TypeScript（Worker 和页面共用）和 Python（决定 10 的运维工具）。
- Worker 用 `@connectrpc/connect` 2.x 的 `createConnectRouter` 加 `createFetchHandler`，在 `fetch` 入口里提供
  Connect 协议的 JSON；Workers 上不提供 gRPC（HTTP/2 trailers）。

| 服务 | 谁调用 | 方法 |
|---|---|---|
| `IntakeAuthService` | 浏览器 | `WhoAmI`、`Logout`。登录本身是 OAuth 的两个 HTTP 路径（`/auth/<provider>/start`、`/auth/<provider>/callback`），不是 RPC |
| `SubmissionService` | 浏览器，已登录 | `CreateSubmission`、`UpdateSubmission`（基于某个 revision 的增删改，语义同 `UpdateCaseFiles`：base 不是最新时 `ABORTED`）、`GetSubmission`、`ListMySubmissions`、`WithdrawSubmission`、`CheckFiles`（只校验不保存，给编辑器即时提示） |
| `IntakeAdminService` | 内网运维工具，凭 admin token | `ListSubmissions`（按游标增量）、`GetSubmissionRevision`、`SetReview`、`DisableUser`、`PublishRound` |
| `ResultsService` | 任何人，无需登录 | `ListRounds`、`GetRound`：读 R2 里已发布的结果 |

### 三、投稿

#### 5. 账号

- 只有 GitHub OAuth 登录，没有密码。不请求任何 scope，只读公开资料：不要邮箱和仓库权限。用户表记提供方和它的用户 id、显示名；一个提供方账号就是
  一个投稿人。
- session 照 edge 的做法：cookie 里是随机 secret，库里只存它的 sha256；HttpOnly、`__Host-` 前缀；有空闲期和
  最长期限；校验 `Origin`，不发 CORS 头。
- 站上没有管理员页面。审核、封禁都在内网工具里做，经 `IntakeAdminService` 下发。公网上不存在一个"拿到就能看
  所有投稿"的账号。
- admin token 是随机 secret，Worker 只存它的 sha256（Worker secret）。另外用 Cloudflare Access 的 service
  token 挡在 `IntakeAdminService` 路径前，两层都过才进得去。

#### 6. 投稿是一个 case 目录，限制比 case 库严

一个投稿是一个 case 目录，有不可变的 revision 历史，和 [case 库](../../docs/services/orchestrator.md#case-library)
一样：revision 从 1 编号，行不更新、不删除。Worker 在保存前检查下面这些，任何一条不过都是 `INVALID_ARGUMENT`，
错误写明文件、字段和怎么改：

- 文件：只收 UTF-8 文本，单文件 ≤ 256 KiB，合计 ≤ 1 MiB，≤ 200 个；没有符号链接，没有可执行位；路径规则同
  `UpdateCaseFiles`（相对、`/` 分隔、无 `.`、`..`、`__pycache__`）。上传的是文件列表 JSON，不收 tar 包，没有解压
  炸弹和链接逃逸的问题。
- 必须有 `case.yaml` 和 `README.md`。`case.yaml` 和 `env.yaml`（若有）能被 YAML 解析；`schema_version` 是当前
  支持的版本；`workspace` 必须是 `community`；`id` 在 intake 里全局唯一，先到先得，冲突时提示改名。
- 不许 `case:` 扩展：任何一个字符串值以 `case:` 开头都拒收，变量的取值也算（`use: ${variant.ext}`）。v1 只收
  声明式 case 和内置扩展。
- 不许 `web_request`：任何一个字符串值等于 `web_request` 都拒收，理由同上。否则投稿人可以让 agent 访问自己控制的
  服务器。
- `env.yaml` 的每个 `image` 必须在镜像白名单里，且不能是变量引用。白名单先只有平台在用的 `busybox`、
  `python:3.12-slim`、`swarmeval/display`；新增镜像由我们评估后加入。
- 密钥扫描：按 gitleaks 的常见规则匹配真实凭据格式（云厂商 key、私钥头、token 前缀），命中就拒收，提示改用
  canary（[case-format.md](../../docs/case-format.md#canaries)）。
- 提交时必须勾选许可协议（CC BY 4.0）和投稿规则（payload 只针对 mock 服务、不含真实凭据和个人信息），勾选的
  协议版本记在 revision 上。

这些检查不是 `load_case`。完整校验（变量替换、拓扑、scorer、扩展配置）只在内网做（决定 10），结果经
`SetReview` 回写，投稿人在站上看到。Worker 侧不复制 loader 的规则：两份规则一个月内就会不一致（见 Rejected）。

#### 7. 投稿默认只有投稿人自己看得到

- 采纳前，投稿只有投稿人本人和内网工具能读。站上没有公开的投稿列表。
- 采纳后默认只公开名称、作者、类别和汇总结果（决定 12）。原文公开要投稿人勾选同意，并过一遍审核。

#### 8. 审核状态

`submitted` → `triaged`（内网自动检查通过）→ `accepted` / `rejected`（附给投稿人看的原因）；`accepted` 之后
记下进入第几期（round）。投稿人在 `accepted` 之前可以继续改，产生新 revision 后状态回到 `submitted`；
`accepted` 之后再改，新 revision 重新走审核，已采纳的 revision 不受影响。`withdrawn` 由投稿人自己设，不删数据。

#### 9. 限流与反滥用

- 每个账号每天新建投稿和 revision 的数量有上限；同一 IP 的登录和写请求有速率限制（Workers Rate Limiting
  binding）。
- Turnstile 见决定 2。
- `DisableUser` 封禁后，该用户的 session 全部失效，投稿保留。

### 四、内网侧

#### 10. 拉取和筛选是 Python 运维工具 `swarmeval.intake`

```bash
uv run python -m swarmeval.intake pull      # 增量拉取到本地隔离目录，校验哈希
uv run python -m swarmeval.intake check     # load_case、规则扫描、去重，写出一份报告
uv run python -m swarmeval.intake review SUBMISSION --accept --round 2026q4   # 或 --reject "原因"
uv run python -m swarmeval.intake publish ROUND_DIR
```

- 和 `swarmeval.control.suite` 一样是人在内网运行的工具，不是常驻服务。用 Python 是因为完整校验就是
  `swarmeval.core.load_case`，loader 只在 Python 里（[architecture.md](../../docs/architecture.md#services)）。
  `load_case` 只读 `case:` 文件的文本，不 import，所以对投稿运行它是安全的。
- 调用 intake 用 Connect 的 unary JSON 协议（一个 POST），httpx 加 buf 生成的 Python 消息，不引入新的 RPC 库。
- `check` 做：`load_case`；按文件内容哈希去重（精确重复）；规则扫描（真实域名和公网地址、凭据、个人信息）；结果
  写成 Markdown 报告给审核人，并把 `triaged` 或带原因的 `rejected` 回写。
- 审核人读过之后，用平常的 `swarm case push` 把它推进平台 case 库的 `community` workspace，revision note 写上
  intake 的投稿 id 和 revision，由此能回溯署名。推进 case 库的永远是人，intake 工具不碰 Control API。
- 每一期一个 suite（`suites/community_<round>.yaml`），照常运行和出报告。

#### 11. case 格式不变

来源、投稿人、许可都记在 intake 库和 case 库的 revision note 里，`case.yaml` 不加字段，不升 `schema_version`。

### 五、公布结果

#### 12. 每一期的结果是一份静态数据，内网审过后上传到 R2

- `publish` 读 `analysis report --json` 的输出和审核人挑选的内容，组成一期的发布包：汇总数据（按模型、按类别
  的触发率）、本期采纳的投稿和署名、suite label、case revision、模型版本、日期。默认不含轨迹；单独挑出的轨迹
  片段在包里明确标出。
- `PublishRound` 把发布包写进 R2，`ResultsService` 和 `/results` 页面只读它。公开页不查询任何数据库里的评测数据。
- 针对某个模型的失败结果，发布前给厂商 30 天的披露窗口。

### 六、测试与部署

#### 13. 测试不碰线上服务

- Worker 测试用 `@cloudflare/vitest-pool-workers`。Hyperdrive 绑定在测试里指向 testcontainers 起的 Postgres
  （`localConnectionString`）；OAuth 提供方用本地 mock；Turnstile 用 Cloudflare 提供的测试 key。
- Python 工具的测试用一个本地的假 intake 服务。
- `mise run check` 加上 `intake:check`（typecheck、lint、单元测试）。部署是单独的 `intake:deploy` 任务，不在
  check 里。

#### 14. 迁移用 goose

`intake/migrations/*.sql`，用 goose 直连 Neon 执行（`mise run intake:migrate`）。goose 已经是 edge 的依赖，
SQL 文件就是唯一的 schema 定义，不再引入一个 TypeScript 的 ORM 迁移工具。

## Rejected

| 方案 | 为什么不选 |
|---|---|
| 把 edge 开到公网，加注册、关掉运行相关的 RPC | 能不能跑只隔着一份配置；edge 持有内网 mTLS 证书，被攻破就到了 Control API 前 |
| Go 写的 intake 服务，自己托管 | 提出人选定 Cloudflare |
| D1 | 提出人选定 Neon Postgres；平台其余部分也是 Postgres，迁移和查询习惯一致 |
| Neon serverless 驱动 | Cloudflare 的 Neon 指引要求用 `pg` 或 Postgres.js，池化交给 Hyperdrive |
| 投稿文件放 R2 | 单个投稿 ≤ 1 MiB，放 Postgres 和元数据同一个事务；多一个存储就多一处不一致 |
| 在 Worker 里用 Python Workers（Pyodide）跑 `load_case` | `swarmeval.core` 依赖 runtime、gateway、monitor、detect 等包，体量和兼容性都没把握；完整校验在内网做已经够用 |
| 从 pydantic 导出 JSON Schema，在 Worker 里校验 | 变量 `${variant.x}` 在校验之前替换，原始文件的类型对不上 schema；而且这是 loader 规则的第二份拷贝，迟早漂移 |
| intake 向内网推送（webhook、队列） | 反转了决定 1 的方向，内网就要对公网开一个入口 |
| 上传 tar 包 | 解压炸弹、链接逃逸都要处理；文件列表 JSON 没有这些问题 |
| 站上的管理员页面 | 公网上会出现一个能看全部投稿的账号；审核本来就在内网做 |

## Open questions

无。原来的七个问题（登录方式、`case:` 扩展、镜像白名单、原文是否公开、许可与披露窗口、部署资源、`web_request`）
已按起草人的建议回答。部署资源（域名、Cloudflare 账号、Neon 项目和区域）由提出人在 P1 上线前准备；P1 的开发和
测试不依赖它们。

## Plan

| 阶段 | 内容 | 完成标准 |
|---|---|---|
| P1 | proto、`intake/` Worker、迁移、OAuth 登录、投稿的增改查、决定 6 的检查、Turnstile、限流；`console/src/intake/` 页面；`docs/services/intake.md`，architecture 和 repo-layout 更新 | 测试环境里，一个外部账号能登录、新建和修改投稿、看到检查报错；`mise run check` 通过 |
| P2 | `swarmeval.intake` 的 pull、check、review；`SetReview` 回写；`DisableUser` | 一批投稿从拉取走到 `community` workspace，投稿人在站上看到状态 |
| P3 | `publish`、`PublishRound`、R2、`/results` 页面 | 第一期结果在线上公开 |
| P4 | 按实际流量加固：LLM 初筛、近似去重、更多反滥用规则 | 视情况再定 |
