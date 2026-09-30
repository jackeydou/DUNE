# Agent runtime and extensions

The agent loop that drives a run, and the extension API for hooking into it. The code is in
`swarmeval/runtime/`. It runs inside the [orchestrator](services/orchestrator.md)'s worker. The
design and its rejected alternatives are in the
[agent loop spec](../spec/2026-09-29-agent-loop-hooks/README.md).

**Status:** the loop, the hooks, and the extension API below are built and tested against
in-memory fakes. Postgres, model-gateway, and sandboxd are not built yet, so the runtime has only
protocol boundaries for them ([Ports](#ports)). [Not built yet](#not-built-yet) lists what the
spec describes but the code does not do.

## The loop

`RunLoop` runs one fresh run. Agents take turns in `round_robin` order. A turn is one model call
plus every tool call it makes:

1. `before_turn` gate. Queued injections and `Inject` messages are admitted first.
2. Read the context from the store: the first `len` messages of the agent's current generation.
3. `compact_context`. A new message list starts a new generation.
4. `before_model_request`, which may narrow the tool list and change sampling options.
5. The model call. It returns only after the gateway's record has been committed.
6. `after_model_response`, then admit the assistant message.
7. For each tool call: `before_tool_call` gate, execute, record the `ToolCallRecord`,
   `after_tool_result`, then admit the tool message.
8. `after_turn`.

An agent whose response has no tool calls is finished. The run ends when every agent is
finished, when `max_turns` or `max_tokens` is reached (`max_tokens` is checked before each model
call), or when a hook or an action stops it.

### Record, then admit

Content reaches an agent's context in two commits. First the content as observed is recorded:
the model call, or the tool call with its result. Then hooks run, and the content as the agent
will see it is admitted: `messages` rows, an `agent_state` row, and one `intervention` event for
every change a hook made. The observed original is never modified. A difference between what the
gateway recorded and what the agent saw is either explained by an `intervention` event or is a
spoofing signal.

A hook never builds a request-only view of the context. Every context change is a committed
message or a new generation, so a request can always be rebuilt from `messages`.

### Tools

| Kind | Where it runs | Defined by |
|---|---|---|
| `SandboxTool` | sandboxd, in the calling agent's sandbox. Its `build` only turns arguments into an `Exec` | The runtime, or an extension with `runs_in="sandbox"` |
| `WorkerTool` | The worker, with only its extension's `HookContext`. It must not do its own I/O | An extension with `runs_in="worker"` |

Arguments are validated against the tool's pydantic model. Invalid arguments, an unknown tool, a
non-zero exit, and a timeout all become error results the agent sees, and each is recorded. A
`RunConfigError` is raised at construction if an agent lists a tool nobody provides, or a sandbox
tool without having a sandbox.

## Ports

| Protocol | Production implementation | Contract |
|---|---|---|
| `RunStore` | Postgres `runs` schema (not built) | `commit` is atomic and assigns `seq` and `event_id` in order. `context` returns `messages[gen][:len]` from the latest `agent_state` row |
| `ModelClient` | model-gateway client (not built) | `generate` returns after the gateway's record of the call has been committed through the run's `RunWriter` |
| `SandboxExecutor` | sandboxd client (not built) | Runs one `Exec` and returns output plus file and process observations |

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
`config`). It resolves them, checks `api_version`, validates each config, and runs setup. It
refuses the following:

- The same instance listed twice without `as:`.
- Two tools with the same name.
- `case:` references to code in a case directory (spec open question 1).

Hooks run in the order the extensions are listed, and within one extension in registration
order.

### Hooks

| Hook | Kind | Payload | Returns |
|---|---|---|---|
| `on_run_start`, `after_turn`, `on_run_end` | Observe | — | `None` |
| `before_turn` | Gate | `TurnInfo` | `Proceed`, `Skip`, `Inject(messages)`, `Stop(reason)` |
| `compact_context` | Transform | Current messages | `None`, or a new message tuple (new generation) |
| `before_model_request` | Transform | `RequestOptions` | `RequestOptions`. Tools may only be narrowed |
| `after_model_response` | Transform | `AssistantMessage` | `AssistantMessage` |
| `before_tool_call` | Gate | `ToolCall` | `Allow`, `Rewrite(arguments)`, `Block(result)` |
| `after_tool_result` | Transform | `ToolResult` | `ToolResult` |
| `on_event` | Observe | `CommittedEvent` | `None` |

The three kinds combine differently:

- **Transform** hooks chain: each receives the previous one's output. Returning the input
  unchanged records nothing.
- **Gate** hooks stop at the first decision that is not the default (`Proceed` / `Allow`). Later
  gates are not called.
- **Observe** hooks all run.

A changed value or a non-default decision is recorded as an `intervention` event.

`on_event` handlers run in a background task, in `seq` order. They never see events their own
instance caused. Before any hook point the loop waits until the observers have processed every
event committed so far, so an action an observer takes always applies at the next hook point.

### HookContext

| Member | Does |
|---|---|
| `ctx.run`, `ctx.agent`, `ctx.hook` | Run id and agents; the agent this call concerns (`None` for run-level hooks); the current hook |
| `ctx.state` | This instance's state. Changes are committed with the step's next transaction and restored from the store when a run starts |
| `ctx.rng` | `random.Random` seeded from the run seed and the instance id |
| `ctx.emit(name, data)` | Records an `extension` event |
| `ctx.actions.alert(...)` | Records an `alert` event |
| `ctx.actions.stop(reason)` | Stops the run at the next hook point. Recorded as an intervention |
| `ctx.actions.inject(agent_id, content)` | Queues a user message for the agent's next `before_turn`. Recorded as an intervention |
| `ctx.model.generate(request)` | A model call under this instance's identity, recorded like an agent's |
| `ctx.sandbox.exec(sandbox_id, command)` | A command in a sandbox, recorded as `sandbox_exec` and attributed to this instance |
| `ctx.spawn(coro)` | Background work. Its emits and state are committed when it finishes |

### Failure

A hook that raises, times out, or returns the wrong type fails the run. The same applies to a
spawned task that raises. The loop commits a `lifecycle` event with `status: failed`, the
instance id, the hook, and the original error, then raises `ExtensionError` chained to the
cause.

The timeout is the loop's `hook_timeout_s` (default 30 s) unless the extension declares its own
`hook_timeout_s`.

## Not built yet

| Spec item | State |
|---|---|
| Resume, takeover, fork | `RunLoop` refuses a run that already has context. The `awaiting_admit` status exists, and the gateway path is to write it when that path is built |
| `before_deliver`, `on_resume` hooks | Arrive with the Message Bus and with recovery |
| `Pause` decisions and pause actions | Need a resume path |
| `ctx.canaries` | Arrives with `swarmeval/honeypot/` |
| `async` and `event_driven` turn policies, `wall_clock` limit | Only `round_robin`, `max_turns`, and `max_tokens` exist |
| Pausing on infrastructure failure | A failing model client or sandbox raises and ends the run |
