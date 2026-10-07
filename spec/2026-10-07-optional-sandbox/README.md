# sandbox 按需分配：agent 可以不要 sandbox

## Status

draft · 2026-10-07 · 提出人要求 spec 和实现一起做。下面的决定都是起草人的建议，实现按本稿完成；提出人确认后
改为 accepted，Open questions 里的取舍可能改动实现。

## Request

提出人（2026-10-07）："我们现在每一个 case Eval 的时候都强制了要创建一个 sandbox，有很多 case 不需要执行代码，
不需要操作文件，不需要 sandbox 的时候，我们应该可以支持不强制分配 sandbox，不然浪费资源。"

现状：loader 给每个 agent 分配 sandbox，没写 `sandbox:` 的 agent 拿到以自己命名的私有 sandbox
（`swarmeval/core/loader.py` 的 `_sandboxes`），所以每个 case 都要有 `env.yaml` 和 `default` profile。worker
对每个 run 都 `CreateRun`、逐个建容器、做隔离自检、`FinalDiff`、`DestroyRun`。`cases/collusion_pricing` 就是
例子：两个卖家只用扩展提供的 worker 工具和 `send_message`，从不碰 sandbox，`env.yaml` 里却要写一个最小的
busybox profile，注释写着"every agent still gets a sandbox, so it is the smallest"。

runtime 这一层已经能处理没有 sandbox 的 agent：`AgentSpec.sandbox_id` 可为 `None`，列了 sandbox 工具却没有
sandbox 的 agent 在 run 开始时报 `RunConfigError`，事件的 `sandbox_id` 本来就可空。缺的是 case 格式里的写法、
loader 的拓扑规划和 worker 的生命周期。

## Decisions

### 1. agent 写 `sandbox: none` 表示不要 sandbox；`case.yaml` 升到 schema_version 5

```yaml
schema_version: 5
swarm:
  agents:
    - { id: seller_a, prompt: prompts/seller.md, tools: [set_price, send_message], sandbox: none }
    - { id: coder, prompt: prompts/coder.md, tools: [shell] }          # 照旧：私有 sandbox
```

- `none` 是保留值，不是实例名。其他写法不变：不写 `sandbox` 仍是私有 sandbox，`sandbox: <name>` 仍是共享实例。
- 一个 case 可以混用：有的 agent 有 sandbox，有的没有。
- 版本 4 照旧加载，含义不变：v4 里 `sandbox: none` 仍是名为 `none` 的共享实例。按 docs/case-format.md 的
  版本规则，给已有字段加一个新含义要升版本。
- v5 的 `env.yaml` 若声明了名为 `none` 的共享实例，它不可能被引用，按已有规则以"声明了没人用的共享实例"报错，
  错误里说明 `none` 是保留值。

理由：显式写在 case 里，审阅 case 的人一眼能看出哪个 agent 没有执行环境；不改变任何已有 case 的行为。

### 2. 没有 sandbox 的 agent 能做什么，在加载时检查

- 不能列内置的 sandbox 工具（目前是 `shell`），加载时报 `CaseError`，指出 agent 和工具。扩展提供的
  `runs_in="sandbox"` 工具只有加载扩展后才知道，仍由 run 开始时的 `RunConfigError` 拦住（已有）。
- 不能写 `os_user`（没有可以运行命令的系统）和 `sandbox_profile`（已有的"二选一"检查）。
- `send_message`、`web_request`（它在 worker 里执行，本来就不经过 sandbox）、扩展的 worker 工具照常使用。

### 3. 没有 sandbox 的 case 可以没有 `env.yaml`

- 没有任何 agent 需要 sandbox、且 case 没写 `environment:` 时，`env.yaml` 不存在也能加载，环境视为空：没有
  profile、共享实例和 canary。
- 写了 `environment:` 就必须存在，和现在一样，免得写错的路径被悄悄当成"没有环境"。
- `env.yaml` 存在时照常校验：canary 必须放在有 agent 使用的 sandbox 里，共享实例必须有 agent 用。没有 agent
  需要私有 sandbox 时，不再要求 `default` profile（现在也只在需要时才要求）。

### 4. 一个 sandbox 都没有的 run 不调用 sandboxd

- worker 不 `CreateRun`、不建容器、不做隔离自检、不 `FinalDiff`、不 `DestroyRun`。这种 run 用一个空的 sandbox
  执行器：`final_diff` 为空、`destroy` 什么都不做；扩展通过 `ctx.sandbox` 执行命令时报错，经扩展错误使 run
  `failed`，错误说明这个 run 没有 sandbox。
- run 的 `isolation` 留空（Console 显示"—"）。它的含义是"run 的 sandbox 中最弱的运行时"，没有 sandbox 就没有
  这个值；写成 `none` 容易被读成"没有隔离"。
- 有一部分 agent 有 sandbox 的 run：只建用到的 sandbox，隔离自检、最终 diff、sandbox canary 只覆盖这些。
- 接管一个过期的 run 时，worker 仍对它发 `DestroyRun`。sandboxd 按标签删除，对没有容器的 run 是空操作；不为
  这一次调用去查 run 的拓扑。

理由：省下的正是提出人说的资源：容器、镜像拉取、隔离自检的十几条命令和 sandboxd 的一次往返。

### 5. 事件、导出和分析不变

- 事件 schema 不变：没有 sandbox 的 agent 的事件 `sandbox_id` 为空，这一点 schema 本来就允许。
- sandbox canary 只为存在的 sandbox 生成。没有 sandbox 的 agent 不在任何 sandbox 里，所以它看到某个 sandbox 的
  canary 时算作越界（`cross_sandbox` 规则不变）。
- 没有 sandbox 的 fork 不需要恢复文件，`fidelity` 记 `fs_restored`（要恢复的集合为空，恢复是完整的）。

### 6. scorer 的加载检查跟着拓扑调整

- `cross_sandbox` 现在要求至少两个 sandbox。改为：至少有一个 sandbox，且至少有一个 agent 不在某个 sandbox 里
  （包括没有 sandbox 的 agent）。一个 sandbox 加一个没有 sandbox 的 agent，信息也可能越界，这个 scorer 有意义。
- `command` scorer 的 sandbox 必须有 agent 使用（已有），所以没有 sandbox 的 case 写不了 `command` scorer。
- `protected_write`、`canary` 不加新检查：没有 sandbox 时它们只是不会触发，和现在没有受保护路径、没有 canary
  的 case 一样。

### 7. Console 和仓库里的 case

- case 流程图不为 `sandbox: none` 的 agent 画私有 sandbox，agent 详情的 sandbox 一行显示 `none`。
- New case 模板升到 `schema_version: 5`，内容不变（模板的 agent 用 `shell`，需要 sandbox）。
- `cases/collusion_pricing` 迁移到 v5：两个卖家写 `sandbox: none`，删掉 `env.yaml` 和 `environment:`。

## Rejected

- **按 `tools` 自动推断，不列 sandbox 工具的 agent 不分配。** 扩展工具是否 `runs_in="sandbox"` 要加载扩展才
  知道，而 `case:` 扩展只在 `--allow-case-code` 的 worker 里导入，控制面和 loader 都不导入；
  `collusion_pricing` 这种只用扩展 worker 工具的 case 恰恰推断不出来。扩展还能通过 `ctx.sandbox` 在 agent 的
  sandbox 里执行命令，静态看不到。推断也会悄悄改变已有 v4 case 的拓扑和证据（sandbox canary、隔离自检）。
- **case 级开关，如 `swarm.sandboxes: false`。** 表达不了"写代码的 agent 有、评审的 agent 没有"的混合 case。
  作为 agent 级写法之上的默认值可以以后再加，见 Open question 1。
- **`sandbox: false` 或 `sandbox: null`。** `null` 和不写分不开；`false` 让字段变成"名字或布尔"，Python 和
  Console 两边的类型都别扭。`none` 在 YAML 里是普通字符串，读起来也自然。
- **推迟到第一次执行命令时才建 sandbox。** 省的资源相近，但 seed 文件、canary、sandbox canary 和隔离自检都要求
  sandbox 在第一回合之前就绪，推迟会让基线和证据的时序变复杂，而且还是要给 case 留一个"永远不要"的写法。
- **sandboxd 侧池化或复用容器。** 降低的是建 sandbox 的成本，不是不需要时也建的浪费，另一个问题。

## Open questions

1. 只有 agent 级写法时，五个纯对话 agent 要写五次 `sandbox: none`。要不要加一个 case 级默认值（如
   `swarm.default_sandbox: none`），agent 可以再覆盖？实现先不做。
2. 没有 sandbox 的 run，`isolation` 留空（决定 4）。报告或 Console 要不要显式区分"没有 sandbox"和"还没建好"？
   目前从 case 能看出来，没加字段。

## Plan

1. **core。** `CASE_SCHEMA_VERSIONS` 加 5；`sandbox: none` 在 v5 表示没有 sandbox；`Variant.sandbox_of` 可返回
   `None`；`run_spec` 给这种 agent `sandbox_id=None`；决定 2、3、6 的加载检查。退出条件：loader 测试覆盖混合
   case、全无 sandbox 且无 `env.yaml`、v4 的 `none` 仍是实例名、`shell`/`os_user` 被拒、`cross_sandbox` 新规则。
2. **worker。** 没有 sandbox 时不调 sandboxd（决定 4）；有一部分时只建用到的。退出条件：sandboxd 指向不存在的
   地址时，一个全无 sandbox 的 case 能从提交跑到 `done`、打分并导出；混合 case 只建一个 sandbox。
3. **Console、case、文档。** 流程图、New case 模板、`cases/collusion_pricing`；`docs/case-format.md`、
   `docs/services/orchestrator.md`；根目录和 console 的 `CHANGELOG.md`。退出条件：`mise run check` 通过。
