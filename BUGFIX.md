# Bug fixes

## 2026-09-30 — A float literal that overflows fails the event commit

**Symptom.** A model tool call with arguments such as `{"x": 1e400}` made the commit of its
`ModelEvent` or `ToolEvent` fail, which fails the run, instead of storing `{}` next to the raw text.
**Root cause.** `parse_arguments` rejected `NaN` / `Infinity` literals and unsafe integers, but
`json.loads` turns an overflowing float literal into `inf` without calling either hook, and
RFC 8785 cannot hash `inf`.
**Fix.** A `parse_float` hook rejects non-finite results. `swarmeval/events/convert.py`.
**Guard.** `tests/events/test_convert.py::test_arguments_json_cannot_carry_exactly_are_kept_raw`
(`1e400` case).
**Touches.** `seal`'s RFC 8785 error, which still fails the commit for any other source of a
non-finite value. Every place that parses untrusted JSON into a payload needs the same three hooks.

## 2026-09-30 — `compact_context` can start an empty generation

**Symptom.** A `compact_context` hook that returned `()` started a generation with no messages. The
next model request had no messages, and exporting the run raised `KeyError`, because `messages`
held no rows for that generation.
**Root cause.** `HookDispatcher.compact_context` accepted any tuple, including an empty one.
**Fix.** An empty tuple is an `ExtensionError`; `None` keeps the current context.
`swarmeval/runtime/extensions/dispatch.py`.
**Guard.** `tests/runtime/test_extensions.py::test_compacting_to_an_empty_context_fails_the_run`.
**Touches.** Export's lookup of `(agent_id, gen)` in `swarmeval/events/export.py`, which relies on
every generation having at least one message row.

## 2026-09-30 — A tool withheld by `before_model_request` still runs

**Symptom.** An extension narrowed a turn's tools and the request omitted the tool, but when the
model called it anyway (stale or hallucinated), the loop executed it.
**Root cause.** `RunLoop._execute` looked the tool up against the agent's permanent
`AgentSpec.tools`, not against what the request offered.
**Fix.** `_step` passes the request's `options.tools` through `_tool_step` to `_execute`; a call
outside it is an `Unknown tool` error result. `swarmeval/runtime/loop.py`.
**Guard.** `tests/runtime/test_extensions.py::test_a_call_to_a_tool_withheld_this_turn_is_refused`.
**Touches.** The narrowing check in `HookDispatcher.before_model_request` (widening fails the run).
Any new path that executes a tool call must check the offered set, never `AgentSpec.tools`.

## 2026-09-30 — A stop is ignored until the next turn

**Symptom.** When a hook or observer requested a stop mid-step, the remaining tool calls of the
same model response still ran, and a stop seen at `compact_context` or `before_model_request`
still made the model call.
**Root cause.** `stop_reason` was checked only after `before_turn` and between turns, while the
spec says a stop takes effect at the hook point where it is seen.
**Fix.** Check `stop_reason` after committing `compact_context` and `before_model_request`, and
after `before_tool_call` before the tool runs. `swarmeval/runtime/loop.py`.
**Guard.** `test_a_stop_takes_effect_before_the_next_tool_call`,
`test_a_stop_takes_effect_before_the_model_call` in `tests/runtime/test_extensions.py`.
**Touches.** The observer barrier every hook method starts with, which is what makes an observer's
stop visible at that point. A new hook point needs the same check right after its commit.

## 2026-09-30 — Spawned work is dropped at run end

**Symptom.** A coroutine started with `ctx.spawn` that was still running when the loop ended was
cancelled, and its emits and state were never committed, with nothing recorded.
**Root cause.** `HookDispatcher.close` cancelled every spawned task, and `_spawn_done` ignored
cancellation.
**Fix.** `HookDispatcher.settle` waits for observers and spawned work, each task bounded by its
instance's hook timeout; a task still running after that fails the run. The loop settles after
`on_run_end` and after the terminal event. `swarmeval/runtime/extensions/dispatch.py`, `loop.py`.
**Guard.** `test_spawned_work_is_committed_before_the_terminal_event`,
`test_spawned_work_still_running_after_its_timeout_fails_the_run`.
**Touches.** The failure path still cancels spawned work on purpose (`HookDispatcher.fail`); the
run has already failed and is recorded. Next entry: same run-end sequence.

## 2026-09-30 — Observers may miss the terminal lifecycle event

**Symptom.** Whether `on_event` handlers saw the terminal `lifecycle` event depended on
scheduling.
**Root cause.** The loop committed the terminal event and closed the dispatcher, cancelling the
observer task, without waiting for it to process the event.
**Fix.** Settle after the terminal event. After a failure, `HookDispatcher.fail` stops delivery,
so observers deterministically see nothing from the failure on. `swarmeval/runtime/loop.py`,
`swarmeval/runtime/extensions/dispatch.py`.
**Guard.** `test_observers_see_the_terminal_event`, `test_observers_get_no_events_after_a_failure`.
**Touches.** Previous entry. Because settling runs after the terminal event, a late failure adds a
`failed` event after `finished`; the last lifecycle event is the outcome (docs/agent-runtime.md).

## 2026-09-30 — Unclean mount paths pass case loading

**Symptom.** `env.yaml` mounts such as `/`, `/workspace/..`, or `/workspace/` loaded fine and only
failed later, when sandboxd refused them at `CreateSandbox`.
**Root cause.** The case model only checked that a mount path was absolute.
**Fix.** Mount paths must also be clean and not `/`. `swarmeval/core/models.py`.
**Guard.** `tests/core/test_loader.py::test_mount_path_must_be_clean_and_not_root`.
**Touches.** Mirrors `cleanMounts` in `go/internal/sandboxd/service.go`. Change both together.
