# Bug fixes

## 2026-10-05 — The first call after `docker compose up --wait` finds the control plane down

**Symptom.** On a Linux host, `deploy/compose/smoke.sh` failed at its first CLI call:
`PushCase: the control plane did not answer`, edge logging `connect: connection refused` to
`control:7090`. The same call a few seconds later succeeded.
**Root cause.** `control` and `analysis` had no healthcheck, so `up --wait` returned, and edge,
the worker, and analysis started, as soon as their containers started. The control plane runs
its migrations before it listens, which took about two seconds there.
**Fix.** Both have a healthcheck that passes once their port accepts a connection, and the
services that call them depend on `service_healthy`. `deploy/compose/compose.yaml`.
**Guard.** `deploy/compose/smoke.sh` (`mise run deploy:smoke`), whose first CLI call comes right
after `up --wait`.
**Touches.** model-gateway and sandboxd still count as up when their containers start; the
worker reaches them only once a run is claimed. A service that migrates before it listens
needs the same healthcheck.

## 2026-10-04 — `DownloadExport` reads the whole export into memory

**Symptom.** Downloading a large `sample.eval` or `events.parquet` allocated the whole object in
the analysis service before the first chunk was sent; a multi-gigabyte run could exhaust its
memory and take every other request with it.
**Root cause.** The RPC called `ObjectStore.get` and then sliced the bytes into chunks.
**Fix.** `ObjectStore.open` returns the object as a stream, and the RPC reads and sends 1 MiB
at a time, closing the stream when the call ends. `swarmeval/events/export.py`,
`swarmeval/analysis/service.py`.
**Guard.** `tests/analysis/test_service.py::test_exports_are_downloaded_in_chunks`, which fails
any download that calls `ObjectStore.get`.
**Touches.** `Judge`, `GetTrace`, and rule scans still read a run's `events.parquet` whole
(`exports.read_events_table`); they parse it, so streaming does not apply, and the judge refuses
a long transcript on its own. Reported by Codex review on #23.

## 2026-10-04 — The same case pushed from two tools makes two revisions

**Symptom.** After the console saved a revision, `swarm case pull` followed by `swarm case push`
of the unchanged directory made a new revision with the same files.
**Root cause.** A revision was the sha256 of the uploaded archive. Python's `tarfile` pads an
archive to 10,240-byte records and the CLI's Go packer does not, so one directory had two
hashes.
**Fix.** The control plane packs what it unpacked again with `bundles.pack` and hashes and
stores those bytes, for `PushCase`, `SubmitRuns`, and `SubmitSuite`. `swarmeval/control/
case_rpcs.py`, `swarmeval/control/service.py`.
**Guard.** `tests/control/test_case_library.py::test_the_same_files_archived_by_another_tool_are_the_same_revision`.
**Touches.** `case_sha256` is now the canonical bundle's hash, not the upload's; the worker
still fetches by it. `UpdateCaseFiles` already compared repacked bytes to tell whether an edit
changed anything, and still does. Go's `pack` (`go/internal/cli/pack.go`) no longer has to
match Python's byte for byte, only file for file. Reported by Codex review on #22.

## 2026-10-04 — A bundle of exactly 64 MiB is refused by gRPC

**Symptom.** edge accepted a bundle of the documented 64 MiB, and the Control API answered
`RESOURCE_EXHAUSTED` before the service saw it.
**Root cause.** The Control API's `grpc.max_receive_message_length` was the bundle limit itself,
and a request is its bundle plus field framing, the actor, and a note or a suite file.
**Fix.** `MAX_MESSAGE_BYTES` is `MAX_BUNDLE_BYTES` plus 1 MiB, and the service refuses a bundle
over `MAX_BUNDLE_BYTES` itself, as `INVALID_ARGUMENT`. `swarmeval/control/server.py`,
`swarmeval/control/case_rpcs.py`.
**Guard.** `tests/control/test_case_library.py::test_a_bundle_of_the_limit_fits_the_control_apis_messages`.
**Touches.** edge's `MaxBundleBytes` (`go/internal/edge/config.go`) must stay equal to
`MAX_BUNDLE_BYTES`; the headroom is on the Control API's side only. Reported by Codex review
on #22.

## 2026-10-04 — A push to an archived case leaves its bundle in object storage

**Symptom.** Each refused push to an archived case with new content left an object under
`cases/sha256/` that no revision names.
**Root cause.** The bundle was uploaded before `add_revision` checked the archive under the
case's lock.
**Fix.** `_refuse_archived` checks before the upload, in `PushCase`, `SubmitRuns`, and
`SubmitSuite`; the locked check stays, for a case archived in between. `swarmeval/control/
case_rpcs.py`, `swarmeval/control/service.py`.
**Guard.** `tests/control/test_case_library.py::test_an_archived_case_is_hidden_and_takes_nothing_until_unarchived`.
**Touches.** A case archived between the two checks still leaves one object; closing that
needs the upload inside the lock, which would hold a row lock across object-store I/O.
Reported by Codex review on #22.

## 2026-10-03 — A stale owner overwrites the summary its run's new owner wrote

**Symptom.** An owner taken over between the end of its run and its final status (its lease ran
out while it stalled, or its worker restarted) had its `finish` ignored, but still exported the
run's summary. That upload could land after the new owner's and replace the final summary with
one read while the run was unfinished.
**Root cause.** `Queue.finish` returned `None` both for a stale `owner_epoch` and for a run with
no rerun, so `Worker._record` could not tell it had lost the run.
**Fix.** `finish` raises `FencedError` for a stale `owner_epoch`; `_record` then writes no
summary. `swarmeval/control/queue.py`, `swarmeval/worker/worker.py`.
**Guard.** `tests/worker/test_takeover.py::test_an_owner_taken_over_after_its_run_ended_writes_no_status_or_summary`,
`tests/control/test_queue.py::test_a_stale_owner_cannot_finish_or_rerun`.
**Touches.** 2026-10-02 (stale worker overwrites an interrupted run): `finish` still leaves a
finished run alone at the right epoch, but now raises rather than returns `None` at a stale one.
`summary_failed` still returns `None` when stale; it only runs after a successful `finish`.
Reported by Codex review on #14.

## 2026-10-03 — A hung lease renewal keeps runs going past their lease

**Symptom.** After one renewal failed fast, the next attempt started at the two-thirds cutoff
with a timeout of a third of a lease, so a database call that hung kept the worker's runs
executing until their lease ran out, when another worker may take them over.
**Root cause.** The renewal loop checked the cutoff only after an attempt failed, and bounded
each attempt by the renewal interval rather than by the time left before the cutoff.
**Fix.** The cutoff is checked before every attempt, each attempt's timeout is the time left
before it, and failures retry every twelfth of the lease. `swarmeval/worker/leases.py`.
**Guard.** `tests/worker/test_leases.py::test_renewals_that_fail_then_hang_stop_every_run_by_the_cutoff`.
**Touches.** Fencing stays the backstop for an owner whose event loop itself stalls. Reported by
Codex review on #14.

## 2026-10-03 — `--lease-s 0` makes workers take over runs endlessly

**Symptom.** `swarmeval-worker --lease-s 0` (or a negative value) took leases that had run out
as they were set, so serving workers kept taking over each other's runs, and their own.
**Root cause.** The flag was parsed with `float` and never checked.
**Fix.** `lease_seconds` accepts only a finite number of seconds above zero and names the bad
value. `swarmeval/worker/server.py`.
**Guard.** `tests/worker/test_server.py::test_a_lease_is_a_finite_number_of_seconds_above_zero`.
**Touches.** `Leases` and `Queue` still trust the value they are given. Reported by Codex review
on #14.

## 2026-10-03 — A restarted worker leaves its interrupted runs' containers on the host

**Symptom.** After a worker restarted and marked the runs it had owned `interrupted` (or finished
the ones cancelled meanwhile), their sandbox containers and state directories stayed on the host
for good: nothing ever removed them.
**Root cause.** `Worker.recover` only changed the runs' rows and wrote their summaries. Only the
run's own `execute` called `DestroyRun`, and that process was gone.
**Fix.** `recover` asks the worker's sandboxd to remove each finished run's sandboxes, as taking
over a run whose lease ran out does. A sandboxd that cannot is logged as a warning and leaves the
labeled containers. `swarmeval/worker/worker.py`.
**Guard.** `tests/worker/test_takeover.py::test_a_restarted_worker_removes_the_sandboxes_of_the_runs_it_finishes`.
**Touches.** 2026-10-02 (cancelled while the worker was down) and 2026-10-02 (stale worker
overwrites an interrupted run): same `interrupt_owned` path, unchanged. A run whose containers
are on another node than the worker that finishes it still leaves them there.

## 2026-10-03 — A canary a delivery rewrite shows its recipient is missed

**Symptom.** When a `before_deliver` hook replaced a message's content with text holding a
canary, neither the canary extension nor the canary scorer saw it, though the recipient read it.
**Root cause.** Canary search skipped deliveries on the assumption that a delivery repeats its
`msg.send`, which stopped holding once `before_deliver` could rewrite content.
**Fix.** Search the content of each `before_deliver` rewrite (`where: rewritten_message`); its
sightings are never cross-sandbox, since the content came through a declared channel.
`swarmeval/honeypot/canary.py`.
**Guard.** `tests/honeypot/test_canary.py::test_a_token_a_delivery_rewrite_shows_its_recipient_is_a_hit`,
`::test_a_sandbox_token_a_rewrite_carries_through_a_channel_is_not_a_crossing`.
**Touches.** The cross-sandbox exemption for delivered tokens (`delivered`): a rewrite must not
make a channel-carried sandbox token look like a crossing. Unchanged deliveries stay unsearched,
so a send is not counted twice. Reported by Codex review on #12.

## 2026-10-03 — `trace` stops at a missing parent as if it were the root

**Symptom.** For a schema 5 export with an event whose `parent_id` was missing (truncated or
edited export, or a writer regression), `trace` ended the chain there and showed that event as
the run's root.
**Root cause.** The walk stopped at any null `parent_id`, the right rule only for runs before
schema 5, which have few parents.
**Fix.** Under schema 5, a null parent on any event but the run's first raises `TraceError`.
`swarmeval/analysis/trace.py`.
**Guard.** `tests/analysis/test_trace.py::test_a_schema_5_event_with_no_parent_before_the_root_is_refused`,
`::test_a_run_before_schema_5_may_end_its_chain_early`.
**Touches.** The causal-parent rules in `docs/event-log.md#causal-parents`: a new event type that
is a root besides the run's first event would now fail `trace`. Reported by Codex review on #12.

## 2026-10-03 — A worker claims runs while it records a host fault

**Symptom.** After a run failed the isolation self-check, a worker whose summary write was slow
could still claim new runs on the unisolated host when another in-flight run freed its slot.
**Root cause.** `Worker._execute` set the halt reason only after `_record` had written the run's
status and summary, and the serving loop checks the halt reason before each claim.
**Fix.** Set the halt reason as soon as `execute` returns a host fault, before recording.
`swarmeval/worker/worker.py`.
**Guard.** `tests/worker/test_multi_worker.py::test_a_slot_freed_while_a_host_fault_is_recorded_claims_nothing`.
**Touches.** Completes 2026-10-02 (one unisolated host fails every run it claims). Anything that
stops the worker claiming must take effect before the first `await` after the run's outcome.
Reported by Codex review on #11.

## 2026-10-02 — The transcript check lists deliveries as interventions

**Symptom.** A run with no extension had a `transcript_check` whose `interventions` named every
`msg.deliver` an agent read, so the field could not tell a run with interventions from one
without.
**Root cause.** The context walk added the source of every user message it matched to the
explained list, and a delivery is a source, not an intervention.
**Fix.** Only injections and `Inject` decisions are added; deliveries are matched but not listed.
`swarmeval/worker/transcript.py`.
**Guard.** `tests/worker/test_transcript.py::test_an_untouched_run_is_consistent` (asserted the
old behavior; now asserts `interventions == ()`).
**Touches.** The new `delivery` check adds `before_deliver` verdicts to the same list; anything
that adds a source of context messages must decide whether it is an intervention.

## 2026-10-02 — One unisolated host fails every run it claims

**Symptom.** A worker whose sandboxd was misconfigured (for example `--sandbox-network
per-sandbox`) failed the isolation self-check on every run it claimed, from every submission, and
kept claiming more, so one broken host turned a whole queue into `failed` runs.
**Root cause.** An `IsolationError` ended only the run; nothing told the worker that the fault was
its host's, not the case's.
**Fix.** `execute` marks the outcome `host_fault`; the worker then claims nothing more, lets its
in-flight runs finish, and `serve` raises `WorkerHalted` naming the run and the probes.
`swarmeval/worker/run.py`, `swarmeval/worker/worker.py`, `swarmeval/worker/server.py`.
**Guard.** `tests/worker/test_multi_worker.py::test_a_run_that_finds_the_host_unisolated_stops_the_worker_claiming`.
**Touches.** The run that found the fault still ends `failed` (not `interrupted`), so it is never
rerun. `drain` stops the same way but returns its outcomes. Any new check of the host itself
should set `host_fault` rather than fail runs one by one.

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

## 2026-10-02 — A summary failure after a rerun was queued marks the run `failed`

**Symptom.** If a run's summary export failed after the run was finished `interrupted` (with its
rerun queued) or `cancelled`, the run was then set `failed`, while its rerun stayed queued: a
failed run that was rerun, against "failed runs are never rerun".
**Root cause.** The worker recorded the summary failure with a second `finish(..., "failed")`,
which overwrote any status.
**Fix.** `Queue.summary_failed` turns only a `done` run `failed` and adds the reason to the
error; other statuses stay. `swarmeval/control/queue.py`, `swarmeval/worker/worker.py`.
**Guard.** `tests/worker/test_reruns.py::test_a_summary_that_cannot_be_written_keeps_an_interrupted_run_and_its_rerun`,
`tests/control/test_queue.py::test_a_missing_summary_fails_only_a_done_run`.
**Touches.** 2026-10-02 (worker stopped while recording): that path is still shielded by
`_to_the_end`. `finish` now refuses finished runs (next entry), so the old second `finish` would
have silently done nothing; don't go back to it.

## 2026-10-02 — A stale worker process overwrites a run its restart interrupted

**Symptom.** After a worker restarted with the same id and marked its old run `interrupted`
(queuing a rerun), a process of the old worker that was still running finished the run late as
`done`, overwriting `interrupted` while the rerun stayed queued.
**Root cause.** `interrupt_owned` left `owner_epoch` unchanged, so the stale owner still passed
the epoch check; and `finish` overwrote any status the epoch matched.
**Fix.** `finish` changes a run only while it is `running`, `paused`, or `cancelled`;
`interrupt_owned` increments `owner_epoch` for the runs it finishes, so the stale owner's next
write raises `FencedError`. A serving worker also checks its id lock every 10 s and stops with
`WorkerIdLost` once the connection holding it is gone. `swarmeval/control/queue.py`,
`swarmeval/worker/worker.py`.
**Guard.** `tests/control/test_queue.py::test_a_stale_owner_cannot_overwrite_a_run_its_restarted_worker_interrupted`,
`test_finish_leaves_an_already_finished_run_alone`,
`tests/worker/test_multi_worker.py::test_a_worker_that_loses_its_id_lock_stops_serving`.
**Touches.** 2026-10-02 (cancelled while the worker was down): those runs are fenced the same
way, and `finish` still keeps `cancelled`. M3's takeover must bump `owner_epoch` too. Up to 10 s
after its lock connection drops a worker is still unguarded.

## 2026-10-02 — A sandboxd refusal ends a run `interrupted` and is rerun

**Symptom.** A run sandboxd refused with `INVALID_ARGUMENT` (a bad user, seed file, environment
variable, hostname, or machine id) ended `interrupted`, so it was rerun up to `epochs` times,
every attempt refused the same way.
**Root cause.** `service_failure` mapped every `SandboxdError` to `interrupted`.
**Fix.** `INVALID_ARGUMENT` ends the run `failed`; `NOT_FOUND`, `UNAVAILABLE`, and every other
code still interrupt it. sandboxd uses no `FAILED_PRECONDITION`. `swarmeval/worker/run.py`.
**Guard.** `tests/worker/test_outcomes.py::test_a_backend_refusal_fails_the_run_and_an_outage_interrupts_it`
(`INVALID_ARGUMENT`, `NOT_FOUND`, `INTERNAL`, protocol-error cases).
**Touches.** 2026-10-02 (model-gateway `4xx` ends a run `interrupted`): the same rule for the
other service. M3's pause must not pause on `INVALID_ARGUMENT`. The rerun cap stays the backstop.

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
