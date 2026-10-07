# 模型在运行时选择：case 只声明模型槽位

## Status

accepted · 2026-10-06 · 决定 1、2、3、6、12 和 Open questions 1、2、3、5 由提出人选定（2026-10-06）；其余决定是
起草人的建议。Open question 4 未回答，实现只做笛卡尔积。

## Request

提出人（2026-10-06）："case.yaml 里面我不希望有 model 的配置，我想把 model 的配置放到平台上，在 run case 的时候
选择。现在的做法让 case 锁死绑定了某个/某些模型，这不是我想要的。"

现状：每个 agent 必须写 `model`（`swarmeval/core/models.py` 的 `AgentDef.model`），惯例是
`model: ${variant.model}` 加一条 `variants.model: [...]` 默认值。提交时可以用 `-V model=...` 或 Console 的
"Variant overrides" 换掉这条轴，suite 的 `models:` 也是去填这条轴。所以模型已经能在运行时换，但 case 仍然写着
具体的模型名，一个部署的模型名会写进 case 本身（`cases/minimax-m3` 分支把两个 case 的默认值改成
`minimax-m3`，就是这个问题）。平台也没有地方列出可选的模型，只能手写 overrides。

提问时确认的三点（2026-10-06）：多 agent 用命名槽位分配模型；已有的 case 直接断掉，不做兼容；可选模型列表仍由
model-gateway 的配置决定。

## Decisions

### 一、case 格式

#### 1. agent 不写模型名，只写槽位

提出人选定（2026-10-06）。

- `AgentDef.model` 删除，换成可选的 `model_slot: <name>`，名字规则和其他 id 相同（`[a-z][a-z0-9_]*`）。不写时
  是 `default`。
- 一个 case 的槽位集合 = 各 agent 的 `model_slot`，按 agent 的顺序第一次出现排列。槽位只管 agent；扩展自己调用的
  模型见决定 2。只有一个 agent 的 case、所有 agent 用同一个模型的 case，什么都不用写，只有一个 `default` 槽位。
- 例：攻击方和防守方用不同模型。

```yaml
swarm:
  agents:
    - { id: attacker, model_slot: attacker, prompt: prompts/attacker.md, tools: [shell] }
    - { id: defender, model_slot: defender, prompt: prompts/defender.md, tools: [shell] }
```

理由：case 描述的是"谁和谁用同一个模型、谁和谁不同"，这是实验设计的一部分，属于 case；具体用哪个模型是这一次
运行的选择，属于平台。槽位正好把两者分开。

#### 2. `paraphrase` 的模型写在 case 里

提出人选定（2026-10-06，回答 Open question 5）。

- `swarmeval.bus.paraphrase` 的 `model` 保持原样：必填，写模型名。改写器是实验装置的一部分，不是被测对象，固定在
  case 里，和 prompt 一样。
- 想比较不同的改写器，用变体轴：`model: ${variant.paraphraser}`。
- 提交时这个名字和槽位选的模型一样向 gateway 校验（决定 5）。loader 把 case 里写死的模型名（目前只有
  `paraphrase` 的 `model`）列出来，供控制面校验。
- 不提供 `${model.<slot>}` 这类引用，扩展不能使用 agent 的槽位。

#### 3. `case.yaml` 升到 schema_version 4，版本 1–3 不再加载

提出人选定（2026-10-06），推翻 AGENTS.md "Case format" 的默认做法（保留读取旧版本）。

- loader 只接受 4。版本 1–3 的 case 报 `CaseError`：说明 `model` 已删除，改写法是删掉 `model`、删掉
  `variants.model`、需要不同模型的 agent 加 `model_slot`，链接 `docs/case-format.md`。
- 4 的其余语法与 3 相同。
- 名为 `model` 的变体轴不再有任何特殊含义，可以照常用来变 prompt 等，但不再填任何模型。
- case 库里已有的 v1–3 修订保留，能看、能 pull，提交运行会因为加载失败被拒绝，错误就是上面这条。已经跑完的 run
  不受影响：事件日志、导出和报告不重新加载 case。
- 同一个改动里迁移 `cases/scorer_misbelief`、`cases/collusion_pricing`、Console 的 New case 模板和
  `suites/m1_core.yaml`。`collusion_pricing` 的改写器现在写的是 `${variant.model}`，与卖家同一个模型；迁移后改成
  固定的 `minimax-m3`（提出人选定，2026-10-06），不再跟随卖家。`cases/minimax-m3` 分支合并前要按同样的方式迁移，它对默认模型的修改随之作废。

### 二、提交

#### 4. 提交时按槽位选模型，每个槽位必填，没有默认值

- 内部 `SubmitRunsRequest` 和对外 `swarmeval.api.v1.SubmitRunsRequest` 新增
  `map<string, ModelChoice> models`，`ModelChoice { repeated string names = 1; }`。键是槽位名。
- case 的每个槽位都必须给出至少一个模型；多给了 case 没有的槽位、或少给，都以 `INVALID_ARGUMENT` 拒绝，错误列出
  case 的全部槽位。平台不设默认模型。
- 每个槽位的模型列表是一条矩阵轴，和变体轴做笛卡尔积：槽位在前（变化最慢），按决定 1 的顺序，然后是 `variants`
  的轴。每个组合 × epoch 是一个 run。
- 存储：`control.run_specs` 加一列 `models`（JSONB，槽位 → 名字列表），记录提交时的选择；`task_args` 里加
  `model.<slot>` → 这个 run 用的模型名。变体轴名不能含 `.`，不会冲突。worker 用 `overrides` + `models` 重新
  加载 case，再按 `variant` 下标取组合，和现在的做法一致。
- 补跑沿用源 run 的 `models`。`ForkRun` 默认也沿用，可以换掉某些槽位的模型（决定 12）。

理由：没有默认值是这次改动的目的本身，平台默认模型只是把绑定从 case 挪到部署配置里。模型进 `task_args`，是因为
报告和 Inspect 导出按 `task_args` 区分变体，模型必须是区分维度之一。

> 实现说明（2026-10-06）：`run_specs.models` 存的是这个 run 自己每个槽位用的那一个模型（槽位 → 名字），不是提交时
> 的名字列表。原因是决定 12：fork 换了模型后，再按提交时的列表和 `variant` 下标去取组合，取到的是源 run 的模型；
> fork 的 fork 也要继承换过的模型。worker 用 `overrides` 和这个 run 的模型（每个槽位一个）加载 case，再按
> `task_args` 找到对应的变体。`variant` 下标仍是提交时的组合序号（槽位在前），run id 和报告不变。

#### 5. 控制面在接受提交前向 model-gateway 校验模型名

- `SubmitRuns`、`SubmitSuite`、`ForkRun` 在写任何行之前调 model-gateway 的 `GET /v1/models`，检查槽位选的模型和
  case 里写死的模型（决定 2）。名字不在其中时
  `INVALID_ARGUMENT`，错误给出槽位、写错的名字和 gateway 提供的全部名字。
- gateway 不可达时提交以 `UNAVAILABLE` 失败，不在未校验的情况下接受。
- model-gateway 的调用方白名单加 `control`，只限 `GET /v1/models`；其余接口仍只接受 `worker`、`analysis`。
  控制面新增 `--model-gateway` 地址参数。

> 实现说明（2026-10-06）：参数名用 `--gateway-http`，和 worker 的同名参数一致。

理由：现在写错模型名，要等 run 排队、建好沙箱、发出第一次模型调用才失败，而且每个 run 失败一次。放到平台上选以后，
提交就是该校验的边界。

#### 6. 可选模型列表来自 model-gateway 的配置

提出人选定（2026-10-06）。

- 对外 `RunService` 加 `ListModels`：edge → 控制面 `ListModels` → gateway `GET /v1/models`，只返回名字。
- 加模型、删模型仍然是改 gateway 的配置文件、重启 gateway，平台不存模型表。

#### 7. case 修订带上它的槽位

- 控制面在 push 和编辑时已经加载 case，顺带算出槽位列表，存在 `control.case_revisions` 上，`GetCaseRevision` 和
  `GetCase` 返回 `model_slots`。Console 和 CLI 据此生成选择表单，不需要自己解析 yaml。
- 提交目录 bundle（不经过库）时，槽位不对，报错里会列出槽位。

> 实现说明（2026-10-06）：槽位没有存进 `control.case_revisions`，而是 `GetCaseRevision` 读取时现算：它本来就要取
> bundle，加载一次 case 的开销很小，也省了一次迁移和一份要和 bundle 保持一致的副本。加载失败的修订（如 v1–3 的）
> 不报错，`model_slots` 为空，另返回 `load_error` 说明为什么不能运行，Console 据此提示。`GetCase` 不返回槽位。

### 三、CLI、Console、suite

#### 8. CLI

- `swarm run CASE -m qwen3-8b,glm-5` 填 `default` 槽位；`-m attacker=a,b -m defender=c` 填命名槽位，可重复。
- 缺槽位时报错列出 case 的槽位，并提示 `swarm models`。
- 新增 `swarm models`，列出 gateway 提供的模型名。
- `-V` 保持原样，只管变体轴。

#### 9. Console

- Case 详情页的运行表单：每个槽位一个多选框，选项来自 `ListModels`，必填。"Variant overrides" 输入框保留，只管
  变体轴。
- New case 模板去掉 `model` 和 `variants.model`，`schema_version: 4`。
- Run 列表和详情显示每个槽位用的模型（读 `task_args` 里的 `model.<slot>`）。

#### 10. suite 升到 schema_version 2，版本 1 不再加载

- 理由同决定 3：v1 的 `models:` 含义是"填 `model` 变体轴"，这条轴已经没有特殊含义。
- `models:` 有两种写法：列表，填每个 case 的 `default` 槽位；映射（槽位 → 列表），按名字填。
- case 条目可以有自己的 `models:`（同样两种写法），按槽位覆盖 suite 级的值。
- 加载 suite 时，任何一个 case 有槽位没被填上，就报 `SuiteError`，指出条目和槽位。
- 映射里出现某个 case 没有的槽位不算错（suite 级的映射本来就跨多个 case），但一个槽位在所有 case 里都没有时报错，
  以免拼错的槽位名被静默忽略。

### 四、不变的部分

#### 11. 事件、导出、分析

- 事件 schema 不变：runtime 的 `AgentSpec.model` 仍然是解析后的模型名，事件里记录的模型名和现在一样。
- Inspect 导出不变：`metadata.models`（agent → 模型）和 `eval.model`（第一个 agent 的模型）照旧，`task_args`
  多出 `model.<slot>` 键。
- 报告按 `task_args` 分组，自动按模型区分；`swarm report --compare model.attacker=a,b` 可以直接比较两个模型。
- 旧 run 的 `task_args` 里是 `model`，新 run 是 `model.default`，跨这次改动的报告在模型维度上不会合并。新旧 case
  的修订号和 sha256 本来就不同，报告也不会把它们算成同一个变体，所以这不是新问题。

> 实现说明（2026-10-06）：决定 12 让 fork 多了一种 `intervention` 动作 `replace_model`，事件 schema 因此升到 9
> （`swarmeval/events/convert.py`），说明里也记了 `task_args` 的 `model.<slot>`。只是新增，版本 8 及更早的 run
> 照旧读取；transcript 检查原先把 `hook: fork` 里除 `edit_context` 外的都当成投递改写，改成只认 `deliver`。

#### 12. fork 可以换掉槽位的模型

提出人选定（2026-10-06），回答 Open question 3。

- `ForkRunRequest` 新增 `map<string, string> models`：槽位 → 一个模型名，从 fork 点起替换该槽位的模型。没列出的
  槽位沿用源 run 的模型。槽位名必须是 case 的槽位，模型名按决定 5 校验。
- 每个替换记一个 `intervention` 事件，`hook: fork`，和其他 fork 编辑一样挂在 fork 的 `started` 下，写明槽位、
  原模型和新模型；`fork_edits` 里也有这一条。
- fork 的 `task_args` 里 `model.<slot>` 是新模型。fork 点之前的轨迹是原模型生成的，这一点由 `forked_from` 和上面的
  事件记录，分析时据此区分。
- 从 fork 点起，换过模型的 agent 的上下文（含原模型的 `reasoning_content`）原样交给新模型；gateway 已经按模型规整
  回传的推理内容，不另做转换。
- CLI：`swarm runs replay RUN --fork-at EVENT -m attacker=glm-5`。Console 的回放页 fork 表单里，每个槽位一个下拉框，
  默认是源 run 的模型。

理由："同样的前缀，换一个模型继续"能回答"是情境把模型推到这一步，还是这个模型自己会这么做"，和替换消息是同一类
反事实。

> 实现说明（2026-10-06）：替换做成一种 fork 编辑 `ForkEdit.replace_model { slot, model }`，而不是
> `ForkRunRequest` 上单独的 `models` 字段：它和其他编辑一样在 `fork_edits` 里留底、在 fork 开始时记成事件，CLI 的
> `--edit` 文件和 Console 的编辑列表也能直接写它。同一个槽位替换两次以 `INVALID_ARGUMENT` 拒绝。CLI 命令是
> `swarm replay RUN --fork-at EVENT -m [SLOT=]MODEL`（`replay` 是顶层命令）。Console 的下拉框默认"keep 源模型"。

## Rejected

- **case 里加 `model_slots:` 段，给槽位写说明。** 提出人认为不需要（2026-10-06，Open question 1）。Console 表单
  只显示槽位名，槽位名本身要起得能看懂。

- **所有 agent 共用一个模型。** 表达不了"弱模型攻击、强模型防守"这类非对称实验（提出人选择了槽位）。
- **运行时按 agent id 逐个选模型。** agent 多时表单很长；几个 agent 共用一个角色时只能逐个设置；suite 的模型矩阵
  也写不出跨 case 通用的形式。槽位在只有一个槽位时退化成"一个模型"，在每个 agent 一个槽位时退化成"按 agent 选"，
  覆盖了两者。
- **保留现状，case 里写默认值，运行时覆盖。** 这正是提出人不想要的绑定，部署相关的名字会进入 case（minimax 分支
  就是这样）。
- **继续读取 v1–3，把 `model: ${variant.model}` 自动映射成槽位。** 提出人选择直接断掉（决定 3）。
- **数据库里的模型注册表，在 Console 里增删模型。** 工作量大很多，还牵涉 gateway 的密钥管理（提出人选择读 gateway
  配置）。
- **槽位声明能力要求（需要工具调用等），提交时过滤可选模型。** 测试名单里的模型都要能做 agent，一定支持工具调用，
  不支持的模型不会进 gateway 配置（提出人，2026-10-06，回答 Open question 2）。
- **`paraphrase` 删掉 `model`，改用槽位，run 时选改写器的模型。** 提出人认为改写器可以固定在 case 里
  （2026-10-06）：它是实验装置，不是被测对象。
- **扩展配置里用 `${model.<slot>}` 引用 agent 的槽位。** 目前唯一调模型的扩展是 `paraphrase`，它的模型固定在
  case 里（决定 2），这个引用没有使用者。
- **把模型选择塞进 `overrides`，用 `model.<slot>` 作键。** 不用加列，但会把两种输入混在一起，`overrides` 的
  "替换已声明的变体轴"语义也要多一个例外。加一列更直白。
- **平台设默认模型，槽位没选时使用。** 只是把绑定从 case 挪到了部署配置里。
- **只在运行时校验模型名（现状）。** 写错一个名字，要等每个 run 都排队、建沙箱、首次调用后才失败。

## Open questions

1. ~~case 要不要有一个可选的 `model_slots:` 段，给每个槽位写说明？~~ 已定（2026-10-06）：不加，见 Rejected。
2. ~~槽位要不要能声明要求（如需要工具调用）？~~ 已定（2026-10-06）：不要，见 Rejected。
3. ~~`ForkRun` 要不要允许换掉某个槽位的模型？~~ 已定（2026-10-06）：允许，见决定 12。
4. 多个槽位之间是笛卡尔积（决定 4）。要不要支持显式列出组合（`[{attacker: a, defender: c}, ...]`），只跑指定的
   配对？实现先只做笛卡尔积。
5. ~~`paraphrase` 的 `model` 要不要强制写成槽位引用？~~ 已定（2026-10-06）：不用槽位，模型固定在 case 里，见
   决定 2。

## Plan

1. **core。** `AgentDef.model_slot`、schema_version 4、拒绝 1–3；槽位收集；列出 case 里写死的模型名；
   `load_case(dir, overrides, models)` 产出槽位 × 变体的组合；`run_spec` 用解析后的模型名；suite v2。迁移仓库里的
   case 和 suite，更新 `docs/case-format.md`。退出条件：`mise run check` 通过，loader 和 suite 的测试覆盖槽位、
   缺槽位、多槽位、v1–3 被拒的错误信息。
2. **控制面和 gateway。** `run_specs.models` 列的迁移；`SubmitRuns`、`SubmitSuite`、`ForkRun` 接收并存储
   `models`，`task_args` 带 `model.<slot>`；向 gateway 校验；`ListModels`；case 修订的 `model_slots`；gateway
   白名单加 `control`（仅 `GET /v1/models`）；`ForkRun` 换模型（决定 12）。退出条件：用 mock gateway 的测试覆盖
   未知模型、gateway 不可达、缺槽位，worker 能按存储的 `models` 重新加载出同一个组合，fork 换模型后从 fork 点起
   调用新模型并留下 `intervention` 事件。
3. **edge、CLI、Console。** 对外 proto 加字段和 `ListModels`（只加不删，`buf breaking` 通过）；`swarm run -m`、
   `swarm models`、`swarm runs replay -m`；运行表单、fork 表单、New case 模板、run 列表和详情。退出条件：Console 的 e2e 能在不写任何模型名的 case 上
   选模型并运行；CLI 能完成同样的操作。
4. **文档和收尾。** `docs/case-format.md`、`docs/services/orchestrator.md`、`docs/services/model-gateway.md`、
   `docs/architecture.md` 的服务身份表、`docs/development.md` 的运行示例；各项目的 `CHANGELOG.md`（标明 Breaking）；
   发布后把本 spec 标为 frozen。
