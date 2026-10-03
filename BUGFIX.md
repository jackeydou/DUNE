# Bug fixes

## 2026-10-02 — The DNS probe puts a peer's canary token in another sandbox

**Symptom.** Sandbox A's isolation `check` script carried sandbox B's hostname, which is B's
sandbox canary token, on its command line, and its `isolation_probe` event stored it, so the token
was in A's `/proc` and in A's events where an offline scan would read it as crossing sandboxes.
**Root cause.** `probe_dns` took each peer name whole, and printed the names in its results.
**Fix.** Names are passed in two halves with a label, joined inside the script, and scrubbed from
what it prints. `swarmeval/worker/probes.py`.
**Guard.** `tests/worker/test_probes.py::test_no_probe_event_carries_another_sandboxs_canary_token`;
the live tests in `tests/worker/test_probes_live.py` check stored events too.
**Touches.** The marker halves rule in the same module (the `/proc` scan must not find its own
command line). Anything else that passes one sandbox's identity into another must split it too.

## 2026-10-02 — A worker stopped while recording a run leaves it without a summary

**Symptom.** Stopping a worker right after a run's final status was written, but before its
summary was, left a `done` (or `failed`, `interrupted`) run that no report listed and no restart
revisited. Found by the multi-worker test, which stops its workers once every run is finished.
**Root cause.** `Worker._execute` awaited `finish` and `export_summary` directly, so a cancel
between them ended the task.
**Fix.** Both run in `_to_the_end`, which shields them from a cancel and lets the cancel through
once they are done. `swarmeval/worker/worker.py`.
**Guard.** `tests/worker/test_multi_worker.py::test_a_worker_stopped_while_recording_a_run_still_writes_its_summary`.
**Touches.** The summary-failure path in the same step, which ends the run `failed`. A worker
killed outright in that window still loses the summary; M3's recovery should check for it.

## 2026-10-02 — A model-gateway `4xx` ends a run as `interrupted`

**Symptom.** A run whose agent names a model the gateway has no route for (`404
model_not_found`), or whose request the gateway refused (`400`), ended `interrupted`. With
reruns, each such run is queued again until its variant runs out of reruns, every attempt
failing the same way.
**Root cause.** `service_failure` treated every gateway status except `502` as an outage.
**Fix.** Any `4xx`, like `502`, ends the run `failed`; `503`, other `5xx`, and an unreachable
gateway still interrupt it. `swarmeval/worker/run.py`.
**Guard.** `tests/worker/test_outcomes.py::test_a_backend_refusal_fails_the_run_and_an_outage_interrupts_it`
(`404` and `400` cases).
**Touches.** Extends 2026-10-01 (backend error ends a run `interrupted`); `502` stays `failed`.
M3's pause must still pause only on `503`, other `5xx`, and unreachable gateways or sandboxd.
The rerun cap in `swarmeval/control/queue.py` is the backstop for any outage that is really
permanent.

## 2026-10-02 — A run cancelled while its worker was down never finishes

**Symptom.** A running run cancelled through the Control API while its worker was down kept
`finished_at` empty and never got a summary after the worker restarted, so reports did not list
it.
**Root cause.** `Queue.interrupt_owned` only looked at `running` and `paused` runs; a cancelled
one is finished by its worker, which was gone.
**Fix.** It also sets `finished_at` on the worker's `cancelled` runs that have none, and
`Worker.recover` writes their summaries. `swarmeval/control/queue.py`, `swarmeval/worker/worker.py`.
**Guard.** `tests/control/test_queue.py::test_a_restarted_worker_finishes_only_its_own_runs`.
**Touches.** `CancelRun` sets `finished_at` itself only for queued runs, and `finish` keeps
`cancelled`. Any new path that ends a run must also write its summary.

## 2026-10-01 — A model backend error ends a run as `interrupted`

**Symptom.** When the model backend refused a call (for example, the context outgrew the model's
window), the run ended `interrupted`, the status for platform outages that M2 will resume, though
retrying the same context gets the same answer.
**Root cause.** The worker caught every `ModelGatewayError` together with `SandboxdError` as an
infrastructure failure, without looking at the gateway's status.
**Fix.** `service_failure` maps model-gateway's `502` (`UPSTREAM_ERROR_STATUS`) to `failed` and
everything else to `interrupted`. `swarmeval/worker/run.py`, `swarmeval/gateway/model/client.py`.
**Guard.** `tests/worker/test_outcomes.py::test_a_backend_refusal_fails_the_run_and_an_outage_interrupts_it`,
`tests/worker/test_end_to_end.py::test_a_model_backend_error_fails_the_run`.
**Touches.** The gateway's status table in docs/services/model-gateway.md. M2's pause-and-resume
must keep treating `502` as final and pause only on `503` and unreachable gateways or sandboxd.

## 2026-10-01 — A command scorer's own writes feed the scorers after it

**Symptom.** With a `command` scorer listed before `protected_write` or `canary`, a scoring
script that wrote under a protected path, or into a file, changed the later verdicts: scores
depended on scorer order instead of on what the agents did.
**Root cause.** `FinalStateScoring.run` appended the command scorer's `SandboxExecRecord` to the
events the later scorers read.
**Fix.** The event is still committed but not added to `seen`. `swarmeval/scorers/final_state.py`.
**Guard.** `tests/scorers/test_final_state.py::test_what_a_command_scorer_writes_does_not_feed_later_scorers`.
**Touches.** The final diff, which is deliberately in `seen` (background writes by agents count).
Anything else the worker runs in a sandbox at scoring time must stay out of `seen` too.

## 2026-10-01 — A canary found only in a written file has no evidence

**Symptom.** A `canary` verdict of 1 whose only hit was a file the run wrote had empty
`event_ids`, so the score named no event that caused it.
**Root cause.** The file scan kept each path's last change but not the event that observed it.
**Fix.** Each change is kept with its event id, which a file hit adds to the evidence and the
explanation. `swarmeval/scorers/final_state.py`.
**Guard.** `tests/scorers/test_final_state.py::test_a_canary_copied_into_a_file_is_found_by_reading_it`.
**Touches.** AGENTS.md's rule that a verdict cites its events. `protected_write` already does;
any new scorer must as well.

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
