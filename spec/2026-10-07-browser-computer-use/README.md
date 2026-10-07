# 沙箱里的 browser 和 computer：agent 操作图形桌面和浏览器

## Status

accepted · 2026-10-07 · 方案（沙箱内显示栈，只访问沙箱内）、截图进上下文、事件格式升版本且不为旧格式写兼容层、
坐标约定、fork 处理由提出人选定（2026-10-07）；其余决定是起草人的建议。

## Request

提出人（2026-10-07）："我需要能让 agent 运行的时候给 sandbox 支持上 browser 和 computer 使用的能力。"在三个方案
（A：显示栈装在沙箱里，只访问沙箱内；B：单独的浏览器出口容器上公网；C：等 net-gateway）里选了 A："按照方案 A
就可以，让 agent 在 sandbox 里面可以使用 browser 和 computer"。

提问时确认的四点（2026-10-07）：

- browser 也要能截图。事件格式升版本；"如果兼容要写很多的代码就不兼容了"。
- 先写 spec，然后直接实现。
- 截图喂给 API 模型（Claude、GPT），用屏幕的实际像素坐标。
- 用过 browser 或 computer 的 run，在那之后的事件上拒绝 fork。

## Decisions

### 一、沙箱里的显示栈

#### 1. 显示栈装在沙箱里，隔离不变

沙箱仍然是 `--network none`。浏览器只能访问沙箱内的 `localhost` 和 `file://`，访问不了公网，也访问不了别的沙箱。
隔离默认值（AGENTS.md "Isolation defaults"）一点不变，所以不需要另外签字。上公网的浏览是方案 B / C，不在这里做。

#### 2. profile 的 `display` 打开显示栈

`env.yaml` 的 sandbox profile 加一个可选的 `display`：

```yaml
sandbox_profiles:
  desktop:
    image: swarmeval/display:dev
    fs: [{ path: /workspace }]
    limits: { memory: 2gib, pids: 512 }
    display: { width: 1024, height: 768, url: "http://127.0.0.1:8080/" }
```

- `width`、`height` 默认 1024×768。这个尺寸 Claude 不会缩放，GPT 的 high detail 也不会缩放，所以模型看到的像素
  坐标就是屏幕坐标（提出人选定用像素坐标）。上限 1920×1200。
- `url` 可选：浏览器启动后打开的第一页，默认 `about:blank`。
- 只加键，不改已有键。按 [case 格式的版本规则](../../docs/case-format.md#versioning)，新字段要升版本：
  `env.yaml` 升到 2，`display` 只在 2 里有效，版本 1 照常读。

#### 3. 显示镜像的约定

`deploy/images/display/` 构建 `swarmeval/display`：Debian、Xvfb、一个让每个窗口都最大化的窗口管理器
（matchbox）、xdotool、Playwright 1.63 和它的 Chromium，再加 `swarm-display` 程序。case 作者用
`FROM swarmeval/display` 加自己的应用。镜像必须提供：

- 用户 `swarmdisplay`，以及只属于它、权限 `0700` 的 `/run/swarm-display`。
- `swarm-display start|computer|browser`。
- 沙箱镜像本来就要求的 `sh`、`sleep`、`tr`。

`/etc/swarm-display/start.d/` 里的可执行文件在浏览器打开之前依次以 `swarmdisplay` 运行，用来启动 case 自己的应用，
比如 `127.0.0.1:8080` 上的网店。

#### 4. sandboxd 在建沙箱时启动它

`CreateSandboxRequest` 加 `display`（宽、高、起始 URL）。设置了它时，sandboxd 在容器启动之后、取第一份 manifest
之前，以 `swarmdisplay` 运行 `swarm-display start`。这个命令等显示栈就绪后退出，在 stdout 上打印守护进程的 pid。
超时、退出码非 0、或者输出不是一个 pid，都让 `CreateSandbox` 失败。

显示栈的 `HOME`、`TMPDIR`、浏览器 profile 都放在 `/run/swarm-display` 下，不在任何 key path 里，所以不产生文件
变更事件。一个 key path 如果是 `/run/swarm-display`、在它下面、或者包含它，就拒绝。

#### 5. 显示用户的进程不算任何调用的进程

浏览器每次导航都会起新的渲染进程。要是按现在的规则，它们会记成某次调用留下的进程，之后所有调用的文件变更都会
变成 `ambiguous`。所以 sandboxd 记下守护进程的数字 uid（从 `start` 打印的 pid 查），之后这个 uid 的进程既不进
`processes`，也不参与归属判断。

代价：以 root 运行的 agent 可以把自己的进程切到这个 uid 下藏起来。不过 root agent 本来就能做很多事，这一点写进
文档。镜像里没有终端程序，所以 `computer` 能起的程序只有 Chromium 和 case 作者自己加的。

### 二、工具

#### 6. `computer` 和 `browser` 是 SandboxTool，以显示用户运行

两个都是内置工具，agent 在 `tools` 里列出才有。agent 的沙箱 profile 没有 `display` 时，case 加载失败。

- `Exec` 加 `user`：工具自己指定运行用户，覆盖 agent 的 `os_user`。这两个工具都以 `swarmdisplay` 运行
  `swarm-display computer|browser <json>`。
- `swarm-display` 通过 `/run/swarm-display` 里的 unix socket 把请求交给守护进程。X 服务器有 cookie 认证，浏览器由
  守护进程通过 pipe 控制（Playwright 默认），不开 CDP 端口。所以非 root 的 agent 绕过这两个工具就碰不到屏幕和
  浏览器。

**`computer`**：`screenshot`、`click`（`button`、`count`）、`mouse_move`、`drag`、`type`、`key`、`scroll`、`wait`、
`cursor_position`。坐标是 `[x, y]` 屏幕像素。除了 `cursor_position`，每个动作之后都返回一张截图，文字里写屏幕尺寸。

**`browser`**：`navigate`、`back`、`forward`、`reload`、`snapshot`、`click`、`type`、`select`、`press`、`hover`、
`scroll`、`wait`、`tabs`、`tab_new`、`tab_select`、`tab_close`、`screenshot`。

- 每个动作都返回 URL、标题，以及 Playwright `aria_snapshot(mode="ai")` 产生的页面快照，快照里每个元素带
  `[ref=eN]`。元素用 `ref` 指定，内部通过 `aria-ref=` 选择器定位。Playwright 1.64 发布后换成 `page.get_by_ref`。
- `screenshot` 动作，或者任何动作加 `screenshot: true`，会额外附上视口的截图（提出人要求 browser 也能截图）。

#### 7. 截图怎么出沙箱：`Exec.collect`

`ExecRequest` 加 `collect`：命令结束后，sandboxd 以这次调用的用户读出这些路径上的普通文件，读完就删掉，作为 blob 流回去；
`ExecHeader.collected` 列出每个文件的路径、大小、sha256，以及是否缺失、是否超过上限（8 MiB）。

以调用的用户读，是因为沙箱 drop 了所有 capability，root 读不了别的用户的 `0700` 目录；而且一次调用能收走的，本来就
是它的用户能读到的。两个工具都收 `/run/swarm-display/out/screenshot.png`。截图不写进 key path，所以不产生文件变更事件，agent 也看不到。
worker 在边界上检查每张截图：PNG 签名、IHDR 里的宽高、尺寸上限；不合格的截图变成工具错误。

### 三、图片进上下文

#### 8. 消息带图片引用，不带图片字节

- `ToolResult` 和 `ToolMessage` 加 `images`：`ImageRef(sha256, media_type, width, height)` 的元组。图片字节在
  blob store 里，按哈希引用。`messages` 行和事件里只有引用。
- `after_tool_result` hook 可以改写或去掉图片；改写照常记成 `intervention`。
- 只有工具结果带图片。用户消息、频道消息、系统提示不带。

#### 9. 每次请求最多带最近 3 张图

截图累积很快。`RequestOptions` 加 `max_images`，默认 3：一次请求只带上下文里最后 3 张图，更早的图换成文字
`[image omitted]`。`before_model_request` 可以改这个值；它记在 `ModelEvent` 里，导出时按同样的规则展开 `input`，
所以 `.eval` 里展示的就是模型实际看到的内容。

#### 10. gateway 协议：图片放在 tool 消息里，发给后端时挪到 user 消息

- worker 到 gateway：`tool` 消息的 `content` 可以是内容块列表（`text`、`image_url` 带 `data:image/png;base64,`
  URL）。这是 SwarmEval 自己子集的扩展，gateway 照常校验。
- gateway 到后端：OpenAI Chat Completions 的 tool 消息只能放文本。所以 gateway 在做完 reasoning passback **之后**，
  把 tool 消息里的图片挪到紧跟在这一串 tool 消息后面的一条 user 消息里，每张图前面标上它属于哪次工具调用。放在
  passback 之后，是因为 `within_turn` 以"最后一条 user 消息"为界，挪出来的 user 消息不能把这个边界移动。
- worker 组请求时从 blob store 取图片；同一个 run 里同一个哈希只取一次。

#### 11. 事件格式 10，不写兼容层

- `metadata.swarmeval.schema_version` 升到 10。新增：`ToolEvent` 的 `images`；`ModelEvent` 的 `max_images`；
  `exec` 观测里的 `collected`。
- 新字段都是可选的，所以旧 run 不需要额外代码就照常能读。提出人同意，需要大量兼容代码时可以不兼容；这里不需要。
- `ToolEvent.result` 在 Postgres 里仍然是文本，读它的地方（detect、analysis、transcript）都不用改。
- 导出 `.eval` 时，带图的 `ToolEvent.result` 和 `ChatMessageTool` 变成 `ContentText` 加
  `ContentImage("attachment://<sha256>")`，图片作为 `data:` URI 放进 `EvalSample.attachments`，每张图只放一次。
  这样 Inspect View 可以直接看到截图，`.eval` 不依赖 blob store。

### 四、fork

#### 12. 用过显示工具之后，不能再 fork

fork 只恢复 key path 里的文件，浏览器和桌面的内存状态恢复不了。所以，如果 fork 点之前有执行过的 `browser` 或
`computer` 调用（`executed_arguments` 不为空），就拒绝这次 fork，错误信息里说明原因（提出人选定）。在第一次
显示工具调用之前 fork 照常可以。

## Rejected

- **浏览器放在 worker 里，或者单独的出口容器（方案 B）。** 要放宽隔离默认值，还会多一个事件来源。提出人选了 A。
- **截图走 stdout。** stdout 会转成文本，截图又要再存一份 blob；而且工具自己的文字输出就没地方放了。
- **用 `ReadFile`（docker copy API）读截图。** gVisor 下，copy API 看不到容器 rootfs 覆盖层里的写入。
- **截图写进 key path。** 会产生文件变更事件，agent 也能读到、能改。
- **按 ref 定位只用公开 API（`boxes=True` 加鼠标坐标）。** 元素在视口外时会点错。
- **tool 结果的图片在 worker 里就拆成 user 消息。** 会移动 `within_turn` 的 passback 边界；而且事件里存的上下文
  和模型实际看到的不一致。
- **所有截图都发给模型。** 上下文会无限增长。
- **只按 pid 子树排除显示栈的进程。** Chromium 的一些辅助进程会脱离父进程、被重新挂到 pid 1 上，这样就排除不掉。

## Open questions

（无）

## Plan

1. proto 和 sandboxd：`display`、`collect`、按显示 uid 排除进程。Go 单元测试，以及用真实显示镜像跑的集成测试。
2. 显示镜像和 `swarm-display`。
3. Python：消息和记录、两个工具、sandbox 客户端、gateway 协议和后端转换、事件格式 10、导出、fork 拒绝、case 加载。
4. 文档：sandboxd、agent-runtime、case-format、event-log、model-gateway、architecture，以及 CHANGELOG。

退出条件：`mise run check` 通过；在 docker 里用显示镜像跑通 `computer` 截图和点击、`browser` 打开沙箱内的页面
并点击 ref；`.eval` 里的截图能解析出来。
