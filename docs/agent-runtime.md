# Agent runtime and extensions

The agent loop that drives a run, and the extension API for hooking into it. The code is in
`swarmeval/runtime/`. It runs inside the [orchestrator](services/orchestrator.md)'s worker. The
design and its rejected alternatives are in the
[agent loop spec](../spec/2026-09-29-agent-loop-hooks/README.md).

**Status:** the loop, the hooks, and the extension API below are built and tested against
in-memory fakes, against the Postgres store, and through the model-gateway and sandboxd clients
([Ports](#ports)). [Not built yet](#not-built-yet) lists what the
spec describes but the code does not do.

## The loop

`RunLoop` runs one fresh run. Agents take turns in `round_robin` order. A turn is one model call
plus every tool call it makes:

1. `before_turn` gate. Messages due on the Message Bus, then queued injections and `Inject`
   messages, are admitted first. Messages extensions posted on channels (`ctx.actions.post`)
   are sent now, after this agent's mail was taken, and routed.
2. Read the context from the store: the first `len` messages of the agent's current generation.
3. `compact_context`. A new message list starts a new generation.
4. `before_model_request`, which may narrow the tool list and change sampling options.
5. The model call. It returns only after the gateway's record has been committed.
6. `after_model_response`, then admit the assistant message.
7. For each tool call: `before_tool_call` gate, execute, record the `ToolCallRecord`, route any
   message it sent ([below](#routing-messages)), `after_tool_result`, then admit the tool
   message.
8. `after_turn`.

An agent whose response has no tool calls is finished until a message is due for it on the
Message Bus, which gives it another turn. The run ends when every agent is finished with no
messages waiting, when `max_turns` or `max_tokens` is reached (`max_tokens` is checked before each model
call), or when a hook or an action stops it.

A stop takes effect at the hook point where it is seen: after `before_turn`, `compact_context`,
and `before_model_request`, and before each tool call. Nothing after that point runs, including
the remaining tool calls of the same response. A tool call already executing finishes.

### Routing messages

A message is routed once, right after its `msg.send` commits: for each recipient, in the order
the channel lists them, `before_deliver` decides ([Hooks](#hooks)), and the verdicts commit
together with their interventions and `runs.deliveries` changes. A message delivered as is, or
rewritten, reaches the recipient at the start of its next turn, as a `msg.deliver` carrying the
final content. A dropped message never does. A message held for `n` turns reaches it at the
start of its own turn `t + 1 + n`, where `t` is the number of turns it had started when the
message was routed: `n` of its turns pass without it. A finished agent is woken by a message due
at its next turn; held mail that is not yet due does not wake it, and since a finished agent
takes no turns, mail held for it stays held unless something else wakes it *(proposed)*. A
message still held when the run ends was never delivered: its row stays `delayed` and no
`msg.deliver` is written.

### Record, then admit

Content reaches an agent's context in two commits. First the content as observed is recorded:
the model call, or the tool call with its result. Then hooks run, and the content as the agent
will see it is admitted: `messages` rows, an `agent_state` row, and one `intervention` event for
every change a hook made. The observed original is never modified. A difference between what the
gateway recorded and what the agent saw is either explained by an `intervention` event or is a
spoofing signal; the worker checks this at run end
([transcript check](event-log.md#transcript-check)).

A hook never builds a request-only view of the context. Every context change is a committed
message or a new generation, so a request can always be rebuilt from `messages`.

### Tools

| Kind | Where it runs | Defined by |
|---|---|---|
| `SandboxTool` | sandboxd, in the calling agent's sandbox. Its `build` only turns arguments into an `Exec` | The runtime, or an extension with `runs_in="sandbox"` |
| `WorkerTool` | The worker, with only its extension's `HookContext`. It must not do its own I/O | An extension with `runs_in="worker"` |
| `WebTool` | The worker, through the run's `WebClient`. Its `build` only turns arguments into a `WebRequest` | The runtime: `web_request` |

The runtime provides three tools itself:

- `shell` (`swarmeval.runtime.tools.SHELL`): `cmd` runs with `sh -c` in the agent's sandbox, with
  `timeout_s` from 0 to 600 seconds, 60 by default. The agent sees stdout then stderr, each cut
  at sandboxd's inline limit with a note giving the full size; the full output is in the blob
  store.
- `send_message` (a `RuntimeTool` the loop adds from its Message Bus): `channel` and `content`.
  See [orchestrator.md](services/orchestrator.md#message-bus).
- `web_request` (`swarmeval.runtime.tools.WEB_REQUEST`): `url`, `method` (default `GET`),
  `headers`, `body`, and `timeout_s` up to 120, 30 by default. Sent by the worker, never the
  sandbox; the agent sees the status line, headers, and the body cut at 64 KiB. Only for agents
  that list it. See [orchestrator.md](services/orchestrator.md#web_request).

A `RuntimeTool` runs in the worker without I/O and returns its result with the events it causes,
which commit with the tool call. `BUILTIN_TOOL_NAMES` lists every runtime tool name; pass it to
`load_extensions` so no extension reuses one.

Arguments are validated against the tool's pydantic model. Invalid arguments, an unknown tool, a
non-zero exit, and a timeout all become error results the agent sees, and each is recorded. A
call counts as unknown unless the request that produced it offered the tool, so a tool that
`before_model_request` withheld this turn is refused even though the agent has it. A
`RunConfigError` is raised at construction if an agent lists a tool nobody provides, or a sandbox
tool without having a sandbox.

## Ports

| Protocol | Production implementation | Contract |
|---|---|---|
| `RunStore` | `swarmeval.events.PostgresRunStore` on the `runs` schema ([event-log.md](event-log.md#tables)) | `commit` is atomic and assigns `seq` and `event_id` in order. `context` returns `messages[gen][:len]` from the latest `agent_state` row |
| `ModelClient` | `swarmeval.gateway.model.client.GatewaySession` ([model-gateway](services/model-gateway.md)) | `generate` returns after the gateway's record of the call has been committed through the run's `RunWriter`. The record's `gen` / `length` come from `ModelRequest.gen` and the request's message count |
| `SandboxExecutor` | `swarmeval.sandbox.RunSandboxes` ([sandboxd](services/sandboxd.md)) | Runs one `Exec` under a run-unique `call_id` and returns output plus file and process observations. Blobs the result names are stored before it returns. The loop passes the tool call's id; `ctx.sandbox` passes `ext:<instance>:<n>` |

`RunWriter` is the run's only writer. The loop and the model-gateway stream handler share one
instance, so every event of the run lands in one sequence. Its subscribers (the observer queue)
see committed events in `seq` order.

Tests use the fakes in `tests/runtime/fakes.py`.

## Writing an extension

An extension is a setup function decorated with `@extension`. Setup receives an `ExtensionAPI`
and registers hooks and tools on it.

```python
from typing import Literal

from pydantic import BaseModel

from swarmeval.runtime.extensions import (
    Allow,
    Block,
    ExtensionAPI,
    HookContext,
    ToolCall,
    ToolDecision,
    extension,
)


class Config(BaseModel):
    protected: list[str]
    mode: Literal["block", "alert"] = "block"


class State(BaseModel):
    hits: int = 0


@extension(id="acme.protect_tests", api_version=1, config=Config, state=State)
def setup(ext: ExtensionAPI[Config, State]) -> None:
    cfg = ext.config

    @ext.on("before_tool_call")
    async def guard(ctx: HookContext[State], call: ToolCall) -> ToolDecision:
        if not any(path in call.arguments for path in cfg.protected):
            return Allow()
        ctx.state.hits += 1
        if cfg.mode == "alert":
            ctx.actions.alert("write to a protected path", severity="high")
            return Allow()
        return Block(result="Permission denied")
```

Rules the runtime enforces:

- **Declaration.** `id` is lowercase and dotted. `config` and `state` are pydantic models, and both
  are optional. State fields need defaults.
- **Setup.** Setup is synchronous and does no I/O. It runs once per run, and must register the
  same hooks and tools for the same config. It may decide from the config which hooks to
  register.
- **Registration.** Registration closes when setup returns. A later `ext.on` or `ext.tool` raises
  `ExtensionDefinitionError`.
- **Hook names.** `ext.on(...)` is overloaded per hook name, so pyright checks the handler's
  signature. An unknown name is an error.

Register it under the `swarmeval.extensions` entry point group:

```toml
[project.entry-points."swarmeval.extensions"]
"acme.protect_tests" = "acme_ext.protect_tests:setup"
```

`load_extensions` takes the case's `extensions:` entries (`ExtensionUse`: `use`, optional `as`,
`config`), followed by its channels' `interventions:` expanded into the same form
(`Variant.extensions`, [case-format.md](case-format.md#channel-interventions)). It resolves them,
checks `api_version`, validates each config, and runs setup. It refuses the following:

- The same instance listed twice without `as:`.
- Two tools with the same name.
- `case:` references to code in a case directory (spec open question 1).

Hooks run in the order the extensions are listed, and within one extension in registration
order.

### Built-in extensions

Registered under the same entry point group by this package; a case lists them like any other.

| Id | Hooks | Does |
|---|---|---|
| `swarmeval.canary` | `on_event` | Searches each committed model call, tool result, and message for the run's file canaries (`ctx.run.canaries`) and sandbox canaries (`ctx.run.sandbox_canaries`) and emits a `canary_hit` event per hit: `canary` (a file canary's id, or the sandbox id), `kind` (`file` or `sandbox`), `sandbox_id` (where it was planted), `where`, `event_id`, `agent_id`, `via` (the decodings that revealed it, empty when the token appeared as is), and `cross_sandbox`. Every file canary sighting is a hit; a sandbox canary sighting only when it crosses sandboxes ([rule](services/orchestrator.md#sandbox-canaries)). State: the hit count, and per agent the canaries a delivered message carried to it |
| `swarmeval.env_state` | `on_run_start`, `after_turn` (with `every_turn: true`), `on_run_end` | Runs each configured snapshot command (`id`, `sandbox`, `run`, `timeout_s`) through `ctx.sandbox` and emits an `env.state` event with its output |
| `swarmeval.bus.drop` | `before_deliver` | `channels`, `p`: drops each message on those channels, per recipient, with probability `p` (one `ctx.rng` draw per message and recipient) |
| `swarmeval.bus.delay` | `before_deliver` | `channels`, `turns`: holds each message on those channels for `turns` of the recipient's own turns, or for a count drawn uniformly from `[min, max]` with `ctx.rng` |
| `swarmeval.bus.paraphrase` | `before_deliver` | `channels`, `model`, `prompt`: asks `model`, through model-gateway under the instance's own key, to rewrite each message on those channels, `prompt` as the system message and the message as the user message, and delivers the reply. The default `prompt` keeps every fact and the intent and changes the wording, structure, and formatting (`DEFAULT_PARAPHRASE_PROMPT`). One call per message and recipient, recorded as the instance's `ModelEvent` with the `msg.send` as parent. Its hooks may take 600 s |
| `swarmeval.bus.inject` | `before_turn` | `channel`, `at_turn`, `sender`, `content`: at the start of run-wide turn `at_turn` (every agent's turns counted together), posts `content` on `channel` as if `sender` sent it (`ctx.actions.post`); members read it at their next turn |

The four `swarmeval.bus.*` extensions are in `swarmeval/gateway/bus/interventions.py`. Their
`channels` may be empty, so a variant axis can switch one off (`[]`) without removing it. A
message they leave alone records nothing.

`ctx.run.canaries` holds each file canary placed for the run: `id`, `sandbox_id`, `path`, and
`token`. `ctx.run.sandbox_canaries` holds each sandbox's own canary: `sandbox_id`, `agents` (the
agents that use the sandbox), `token`, and where it is planted: `hostname`, `env_var`, and
`path`.

### Hooks

| Hook | Kind | Payload | Returns |
|---|---|---|---|
| `on_run_start`, `after_turn`, `on_run_end` | Observe | — | `None` |
| `before_turn` | Gate | `TurnInfo` | `Proceed`, `Skip`, `Inject(messages)`, `Stop(reason)` |
| `compact_context` | Transform | Current messages | `None`, or a new, non-empty message tuple (new generation). An empty tuple fails the run |
| `before_model_request` | Transform | `RequestOptions` | `RequestOptions`. Tools may only be narrowed |
| `after_model_response` | Transform | `AssistantMessage` | `AssistantMessage` |
| `before_tool_call` | Gate | `ToolCall` | `Allow`, `Rewrite(arguments)`, `Block(result)` |
| `after_tool_result` | Transform | `ToolResult` | `ToolResult` |
| `before_deliver` | Chain of verdicts | `Envelope`: `send_event_id`, `channel`, `sender`, `recipient`, `content`, `delayed_turns` | `Deliver(content)`, `Drop(reason)`, `Delay(turns)` |
| `on_event` | Observe | `CommittedEvent` | `None` |

The kinds combine differently:

- **Transform** hooks chain: each receives the previous one's output. Returning the input
  unchanged records nothing.
- **`before_deliver`** runs once per message and recipient. A `Deliver`'s content is the next
  handler's input (`Deliver` of the same content changes nothing); `Delay`s add up, and later
  handlers still run and see the total in `delayed_turns`; a `Drop` ends the chain. Each change
  is an `intervention` (`hook` `before_deliver`, `action` `deliver`, `drop`, or `delay`) whose
  parent and `target_event_id` are the `msg.send` and whose `after` is the verdict with
  `recipient`. `Delay` counts the recipient's own turns; delays in seconds wait for the `async`
  turn policy.
- **Gate** hooks stop at the first decision that is not the default (`Proceed` / `Allow`). Later
  gates are not called.
- **Observe** hooks all run.

A changed value or a non-default decision is recorded as an `intervention` event.

`on_event` handlers run in a background task, in `seq` order. They never see events their own
instance caused. Before any hook point the loop waits until the observers have processed every
event committed so far, so an action an observer takes always applies at the next hook point.
Observers see every event up to and including the terminal `lifecycle` event.

### HookContext

| Member | Does |
|---|---|
| `ctx.run`, `ctx.agent`, `ctx.hook` | Run id and agents; the agent this call concerns (`None` for run-level hooks); the current hook |
| `ctx.trigger_id` | The event that caused this call ([table](event-log.md#causal-parents)). What the call records names it as parent unless an action names its own `cause` |
| `ctx.state` | This instance's state. Changes are committed with the step's next transaction and restored from the store when a run starts |
| `ctx.rng` | A `random.Random` for this call. Its seed comes from the run seed, the instance id, and how many of the instance's calls have drawn from `ctx.rng` before; that count is committed with the instance's state, so a resumed or forked run continues the same draws. Randomness in an extension comes only from here |
| `ctx.emit(name, data)` | Records an `extension` event |
| `ctx.actions.alert(message, severity=, event_ids=)` | Records an `alert` event, parented to the last of `event_ids`, and returns its id |
| `ctx.actions.stop(reason, cause=)` | Stops the run at the next hook point. Recorded as an intervention whose parent is `cause` (an event id, such as an alert's), or the trigger; the run's last `lifecycle` event descends from it |
| `ctx.actions.inject(agent_id, content, cause=)` | Queues a user message for the agent's next `before_turn`. Recorded as an intervention, parented like `stop`'s |
| `ctx.actions.post(channel, sender, content, cause=)` | Puts a message on a channel as if `sender` sent it, at the next `before_turn`: every member but `sender` gets it, routed through `before_deliver` like any message. Recorded as a `post` intervention, parented like `stop`'s, and a `msg.send` with no agent, the instance as its `extension`, no `call_id`, and the intervention as parent |
| `ctx.model.generate(request)` | A model call under this instance's identity, recorded like an agent's |
| `ctx.sandbox.exec(sandbox_id, command)` | A command in a sandbox, recorded as `sandbox_exec` and attributed to this instance |
| `ctx.spawn(coro)` | Background work. Its emits and state are committed when it finishes. The run waits for it at the end ([Run end](#run-end)) |

### Run end

After `on_run_end`, and again after the terminal `lifecycle` event, the loop waits until the
observers have processed every event and every spawned task has finished and committed, including
work those tasks spawn. Each task gets its instance's timeout, counted from the start of the wait.
A task still running after that fails the run like a hook timeout. The failure can come after the
terminal event, so a run's log may end `finished` then `failed`; the last `lifecycle` event is the
run's outcome.

### Failure

A hook that raises, times out, or returns the wrong type fails the run. The same applies to a
spawned task that raises. The loop commits a `lifecycle` event with `status: failed`, the
instance id, the hook, and the original error, then raises `ExtensionError` chained to the
cause. From the failure on, observers get no further events, the failed event included, and
spawned work is cancelled without committing.

The timeout is the loop's `hook_timeout_s` (default 30 s) unless the extension declares its own
`hook_timeout_s`.

## Not built yet

| Spec item | State |
|---|---|
| Resume, takeover, fork | `RunLoop` refuses a run that already has context. The `awaiting_admit` status exists, and the gateway path is to write it when that path is built |
| `on_resume` hook | Arrives with recovery (M3) |
| `Delay` in seconds | With the `async` turn policy |
| `read_messages` | Arrives with monitors (M2). Channel members get messages pushed at their next turn |
| `Pause` decisions and pause actions | Need a resume path |
| `async` and `event_driven` turn policies, `wall_clock` limit | Only `round_robin`, `max_turns`, and `max_tokens` exist |
| Pausing on infrastructure failure | A failing model client or sandbox raises and ends the run |
