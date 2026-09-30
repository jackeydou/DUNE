import asyncio
import json
import re

import pytest
from pydantic import BaseModel

from swarmeval.runtime.extensions import (
    Allow,
    AssistantMessage,
    Block,
    ChatMessage,
    CommittedEvent,
    Exec,
    Extension,
    ExtensionAPI,
    ExtensionError,
    ExtensionUse,
    HookContext,
    Inject,
    NoConfig,
    NoState,
    Proceed,
    RequestOptions,
    Rewrite,
    Stop,
    ToolCall,
    ToolDecision,
    ToolMessage,
    ToolResult,
    TurnDecision,
    TurnInfo,
    UserMessage,
    extension,
)
from swarmeval.runtime.messages import ModelRequest, ModelResponse
from swarmeval.runtime.records import (
    InterventionRecord,
    LifecycleRecord,
    ModelCallRecord,
    ToolCallRecord,
)
from tests.runtime.fakes import FakeStore, agent, call, harness, reply

LS = call("shell", '{"cmd": "ls"}')


def two_steps() -> dict[str, list[ModelResponse]]:
    return {"a": [reply("", LS), reply("done")]}


# Rewriting and blocking


@extension(id="t.redact", api_version=1)
def redact(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("after_tool_result")
    async def _(ctx: HookContext[NoState], result: ToolResult) -> ToolResult:
        return result.model_copy(update={"content": "[redacted]"})


async def test_a_rewritten_result_is_what_the_agent_sees_and_both_are_recorded() -> None:
    h = harness((agent(),), two_steps(), extensions=[redact])

    await h.loop.run()

    message = h.store.messages("a")[3]
    assert isinstance(message, ToolMessage) and message.content == "[redacted]"
    tool_event = h.store.records("tool")[0]
    assert isinstance(tool_event.record, ToolCallRecord)
    assert tool_event.record.result.content == "sh -c ls"
    (intervention,) = h.store.records("intervention")
    assert isinstance(intervention.record, InterventionRecord)
    assert intervention.extension == "t.redact"
    assert intervention.record.hook == "after_tool_result"
    assert intervention.record.target_event_id == tool_event.event_id
    assert intervention.parent_id == tool_event.event_id


@extension(id="t.guard", api_version=1)
def guard(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_tool_call")
    async def _(ctx: HookContext[NoState], call: ToolCall) -> ToolDecision:
        return Block(result="Permission denied")


async def test_a_blocked_call_is_not_executed() -> None:
    h = harness((agent(),), two_steps(), extensions=[guard])

    await h.loop.run()

    assert h.sandbox.calls == []
    record = h.store.records("tool")[0].record
    assert isinstance(record, ToolCallRecord)
    assert (record.blocked_by, record.executed_arguments) == ("t.guard", None)
    message = h.store.messages("a")[3]
    assert isinstance(message, ToolMessage)
    assert (message.content, message.is_error) == ("Permission denied", True)
    (intervention,) = h.store.records("intervention")
    assert isinstance(intervention.record, InterventionRecord)
    assert intervention.record.action == "block"


@extension(id="t.sanitize", api_version=1)
def sanitize(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_tool_call")
    async def _(ctx: HookContext[NoState], call: ToolCall) -> ToolDecision:
        return Rewrite(arguments={"cmd": "echo safe"})


async def test_rewritten_arguments_are_what_runs() -> None:
    h = harness((agent(),), two_steps(), extensions=[sanitize])

    await h.loop.run()

    assert h.sandbox.calls[0][2] == Exec(argv=("sh", "-c", "echo safe"))
    record = h.store.records("tool")[0].record
    assert isinstance(record, ToolCallRecord)
    assert record.call.arguments == '{"cmd": "ls"}'
    assert json.loads(record.executed_arguments or "") == {"cmd": "echo safe"}


seen_by_second_gate: list[str] = []


@extension(id="t.second_gate", api_version=1)
def second_gate(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_tool_call")
    async def _(ctx: HookContext[NoState], call: ToolCall) -> ToolDecision:
        seen_by_second_gate.append(call.id)
        return Allow()


async def test_the_first_non_allow_decision_wins_and_stops_the_gate() -> None:
    seen_by_second_gate.clear()
    h = harness((agent(),), two_steps(), extensions=[guard, second_gate])

    await h.loop.run()

    assert seen_by_second_gate == []


def suffix(tag: str) -> Extension[NoConfig, NoState]:
    @extension(id=f"t.suffix_{tag}", api_version=1)
    def setup(ext: ExtensionAPI[NoConfig, NoState]) -> None:
        @ext.on("after_model_response")
        async def _(ctx: HookContext[NoState], message: AssistantMessage) -> AssistantMessage:
            return message.model_copy(update={"content": f"{message.content}-{tag}"})

    return setup


async def test_transforms_chain_in_case_order() -> None:
    h = harness((agent(),), {"a": [reply("x")]}, extensions=[suffix("a"), suffix("b")])

    await h.loop.run()

    last = h.store.messages("a")[-1]
    assert isinstance(last, AssistantMessage) and last.content == "x-a-b"
    model = h.store.records("model")[0].record
    assert isinstance(model, ModelCallRecord) and model.response.content == "x"
    assert [e.extension for e in h.store.records("intervention")] == ["t.suffix_a", "t.suffix_b"]


@extension(id="t.noop", api_version=1)
def noop(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("after_tool_result")
    async def _(ctx: HookContext[NoState], result: ToolResult) -> ToolResult:
        return result.model_copy()


async def test_an_unchanged_value_records_no_intervention() -> None:
    h = harness((agent(),), two_steps(), extensions=[noop])

    await h.loop.run()

    assert h.store.records("intervention") == []


@extension(id="t.no_tools", api_version=1)
def no_tools(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_model_request")
    async def _(ctx: HookContext[NoState], options: RequestOptions) -> RequestOptions:
        return options.model_copy(update={"tools": ()})


async def test_a_request_hook_can_narrow_the_tools() -> None:
    h = harness((agent(),), {"a": [reply("done")]}, extensions=[no_tools])

    await h.loop.run()

    _, request = h.model.requests[0]
    assert request.tools == ()


@extension(id="t.more_tools", api_version=1)
def more_tools(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_model_request")
    async def _(ctx: HookContext[NoState], options: RequestOptions) -> RequestOptions:
        return options.model_copy(update={"tools": (*options.tools, "browser")})


async def test_a_request_hook_cannot_widen_the_tools() -> None:
    h = harness((agent(),), {"a": [reply("done")]}, extensions=[more_tools])

    with pytest.raises(ExtensionError, match="may only narrow"):
        await h.loop.run()


@extension(id="t.compact", api_version=1)
def compact(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("compact_context")
    async def _(
        ctx: HookContext[NoState], messages: tuple[ChatMessage, ...]
    ) -> tuple[ChatMessage, ...] | None:
        if len(messages) <= 2:
            return None
        return (messages[0], UserMessage(content="Summary of earlier steps."))


async def test_compaction_starts_a_new_generation() -> None:
    h = harness((agent(),), two_steps(), extensions=[compact])

    await h.loop.run()

    gens = h.store.generations["a"]
    assert len(gens) == 2
    assert len(gens[0]) == 4
    _, second_request = h.model.requests[1]
    assert [m.role for m in second_request.messages] == ["system", "user"]
    assert [
        e.record.hook
        for e in h.store.records("intervention")
        if isinstance(e.record, InterventionRecord)
    ] == ["compact_context"]


# Turns


@extension(id="t.nudge", api_version=1)
def nudge(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_turn")
    async def _(ctx: HookContext[NoState], turn: TurnInfo) -> TurnDecision:
        if turn.turn == 1:
            return Inject(messages=(UserMessage(content="Remember the deadline."),))
        return Proceed()


async def test_an_injected_message_is_in_the_next_request() -> None:
    h = harness((agent(),), {"a": [reply("done")]}, extensions=[nudge])

    await h.loop.run()

    _, request = h.model.requests[0]
    assert request.messages[-1] == UserMessage(content="Remember the deadline.")


@extension(id="t.halt", api_version=1)
def halt(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_turn")
    async def _(ctx: HookContext[NoState], turn: TurnInfo) -> TurnDecision:
        return Stop(reason="enough") if turn.turn == 2 else Proceed()


async def test_a_stop_decision_ends_the_run() -> None:
    h = harness((agent(),), two_steps(), extensions=[halt])

    outcome = await h.loop.run()

    assert (outcome.status, outcome.reason) == ("stopped", "t.halt: enough")
    assert len(h.model.requests) == 1


# State


class Count(BaseModel):
    calls: int = 0


@extension(id="t.counter", api_version=1, state=Count)
def counter(ext: ExtensionAPI[NoConfig, Count]) -> None:
    @ext.on("before_tool_call")
    async def _(ctx: HookContext[Count], call: ToolCall) -> ToolDecision:
        ctx.state.calls += 1
        return Allow()


async def test_state_changes_are_committed_with_the_step() -> None:
    h = harness(
        (agent(),), {"a": [reply("", LS), reply("", LS), reply("done")]}, extensions=[counter]
    )

    await h.loop.run()

    values = [v for i, _, v in h.store.extension_rows if i == "t.counter"]
    assert values == [{"calls": 1}, {"calls": 2}]


async def test_state_is_restored_from_the_store() -> None:
    store = FakeStore(extension_rows=[("t.counter", 0, {"calls": 41})])
    h = harness((agent(),), two_steps(), extensions=[counter], store=store)

    await h.loop.run()

    assert store.extension_rows[-1] == ("t.counter", 3, {"calls": 42})


# Observers


@extension(id="t.watch", api_version=1)
def watch(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_event")
    async def _(ctx: HookContext[NoState], event: CommittedEvent) -> None:
        if isinstance(event.record, ToolCallRecord):
            ctx.actions.alert("tool used", severity="low", event_ids=[event.event_id])
            ctx.actions.stop("saw a tool call")


async def test_an_observer_action_takes_effect_at_the_next_hook_point() -> None:
    looping = [reply("", LS) for _ in range(5)]
    h = harness((agent(),), {"a": looping}, extensions=[watch])

    outcome = await h.loop.run()

    assert (outcome.status, outcome.turns) == ("stopped", 1)
    assert outcome.reason == "t.watch: saw a tool call"
    (alert,) = h.store.records("alert")
    assert alert.extension == "t.watch"


seen_events: list[str] = []


@extension(id="t.log", api_version=1)
def log(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_event")
    async def _(ctx: HookContext[NoState], event: CommittedEvent) -> None:
        seen_events.append(event.record.kind)
        if isinstance(event.record, ToolCallRecord):
            ctx.emit("tool_seen", {"id": event.event_id})


async def test_an_observer_does_not_see_its_own_events() -> None:
    seen_events.clear()
    h = harness((agent(),), two_steps(), extensions=[log])

    await h.loop.run()

    assert "extension" not in seen_events
    assert [e.extension for e in h.store.records("extension")] == ["t.log"]
    assert seen_events[0] == "lifecycle"


@extension(id="t.whisper", api_version=1)
def whisper(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_event")
    async def _(ctx: HookContext[NoState], event: CommittedEvent) -> None:
        if isinstance(event.record, ToolCallRecord):
            ctx.actions.inject("a", "Someone is watching.")


async def test_an_observer_can_inject_into_the_next_turn() -> None:
    h = harness((agent(),), two_steps(), extensions=[whisper])

    await h.loop.run()

    _, second_request = h.model.requests[1]
    assert second_request.messages[-1] == UserMessage(content="Someone is watching.")
    (intervention,) = h.store.records("intervention")
    assert isinstance(intervention.record, InterventionRecord)
    assert (intervention.record.hook, intervention.record.action) == ("on_event", "inject")


# Failures


@extension(id="t.broken", api_version=1)
def broken(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("after_tool_result")
    async def _(ctx: HookContext[NoState], result: ToolResult) -> ToolResult:
        raise RuntimeError("bug in extension")


async def test_a_failing_hook_fails_the_run_and_is_recorded() -> None:
    h = harness((agent(),), two_steps(), extensions=[broken])

    with pytest.raises(
        ExtensionError, match=re.escape("`t.broken` failed in `after_tool_result`")
    ) as info:
        await h.loop.run()

    assert isinstance(info.value.__cause__, RuntimeError)
    last = h.store.events[-1]
    assert isinstance(last.record, LifecycleRecord)
    assert (last.record.status, last.record.hook, last.extension) == (
        "failed",
        "after_tool_result",
        "t.broken",
    )
    assert "bug in extension" in (last.record.error or "")


@extension(id="t.slow", api_version=1, hook_timeout_s=0.01)
def slow(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_run_start")
    async def _(ctx: HookContext[NoState]) -> None:
        await asyncio.sleep(1)


async def test_a_hook_that_times_out_fails_the_run() -> None:
    h = harness((agent(),), two_steps(), extensions=[slow])

    with pytest.raises(ExtensionError, match=re.escape("timed out after 0.01s")) as info:
        await h.loop.run()

    assert isinstance(info.value.__cause__, TimeoutError)


@extension(id="t.wrong_type", api_version=1)
def wrong_type(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("after_tool_result")
    async def _(ctx: HookContext[NoState], result: ToolResult) -> ToolResult:
        return "oops"  # pyright: ignore[reportReturnType]


async def test_a_hook_returning_the_wrong_type_fails_the_run() -> None:
    h = harness((agent(),), two_steps(), extensions=[wrong_type])

    with pytest.raises(ExtensionError, match="returned str; return a ToolResult"):
        await h.loop.run()


@extension(id="t.background", api_version=1)
def background(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_run_start")
    async def _(ctx: HookContext[NoState]) -> None:
        async def fail() -> None:
            raise ValueError("background failure")

        ctx.spawn(fail())


async def test_a_failing_spawned_task_fails_the_run_at_the_next_hook_point() -> None:
    h = harness((agent(),), two_steps(), extensions=[background])

    with pytest.raises(ExtensionError, match="failed in `spawn`: ValueError"):
        await h.loop.run()


# Tools, model calls, randomness


class ReportArgs(BaseModel):
    summary: str


class RunTestsArgs(BaseModel):
    path: str


@extension(id="t.tools", api_version=1)
def tools_ext(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.tool("submit_report", args=ReportArgs, description="Submit.", runs_in="worker")
    async def _(ctx: HookContext[NoState], args: ReportArgs) -> str:
        ctx.emit("report", args)
        return "Report received."

    @ext.tool("run_tests", args=RunTestsArgs, description="Run tests.", runs_in="sandbox")
    def _(args: RunTestsArgs) -> Exec:
        return Exec(argv=("pytest", args.path))


async def test_extension_tools_run_on_the_same_path_as_builtin_ones() -> None:
    report = call("submit_report", '{"summary": "all good"}')
    tests = call("run_tests", '{"path": "tests/"}')
    h = harness(
        (agent(tools=("submit_report", "run_tests")),),
        {"a": [reply("", report, tests), reply("done")]},
        extensions=[tools_ext],
    )

    await h.loop.run()

    tool_messages = [m for m in h.store.messages("a") if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_messages] == ["Report received.", "pytest tests/"]
    (emitted,) = h.store.records("extension")
    assert emitted.agent_id == "a" and emitted.extension == "t.tools"
    assert h.sandbox.calls[0][2] == Exec(argv=("pytest", "tests/"))


@extension(id="t.judge", api_version=1)
def judge(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_run_end")
    async def _(ctx: HookContext[NoState]) -> None:
        request = ModelRequest(
            model="judge-model",
            messages=(UserMessage(content="Was it fine?"),),
            options=RequestOptions(tools=()),
        )
        response = await ctx.model.generate(request)
        ctx.emit("verdict", {"text": response.message.content})


async def test_extension_model_calls_are_recorded_under_the_instance() -> None:
    h = harness(
        (agent(),),
        {"a": [reply("done")], "judge_1": [reply("yes")]},
        extensions=[(judge, ExtensionUse.model_validate({"use": "t.judge", "as": "judge_1"}))],
    )

    await h.loop.run()

    extension_calls = [e for e in h.store.records("model") if e.extension == "judge_1"]
    assert len(extension_calls) == 1 and extension_calls[0].agent_id is None
    verdict = h.store.records("extension")[0].record
    assert verdict.model_dump()["data"] == {"text": "yes"}


draws: list[float] = []


@extension(id="t.dice", api_version=1)
def dice(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_run_start")
    async def _(ctx: HookContext[NoState]) -> None:
        draws.append(ctx.rng.random())


async def test_randomness_is_seeded_per_run_and_instance() -> None:
    draws.clear()
    for seed in (1, 1, 2):
        await harness((agent(),), {"a": [reply("done")]}, extensions=[dice], seed=seed).loop.run()

    assert draws[0] == draws[1] != draws[2]


# Review fixes: narrowed tools, stops at every hook point, run-end settling


async def test_a_call_to_a_tool_withheld_this_turn_is_refused() -> None:
    h = harness((agent(),), two_steps(), extensions=[no_tools])

    await h.loop.run()

    assert h.sandbox.calls == []
    message = h.store.messages("a")[3]
    assert isinstance(message, ToolMessage)
    assert message.is_error
    assert "Unknown tool `shell`. Available tools: none." in message.content


@extension(id="t.one_is_enough", api_version=1)
def one_is_enough(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("after_tool_result")
    async def _(ctx: HookContext[NoState], result: ToolResult) -> ToolResult:
        ctx.actions.stop("one is enough")
        return result


async def test_a_stop_takes_effect_before_the_next_tool_call() -> None:
    both = reply("", call("shell", '{"cmd": "a"}', id="c1"), call("shell", '{"cmd": "b"}', id="c2"))
    h = harness((agent(),), {"a": [both, reply("done")]}, extensions=[one_is_enough])

    outcome = await h.loop.run()

    assert (outcome.status, outcome.reason) == ("stopped", "t.one_is_enough: one is enough")
    assert [command.argv for _, _, command in h.sandbox.calls] == [("sh", "-c", "a")]
    assert len(h.store.records("tool")) == 1


@extension(id="t.no_model", api_version=1)
def no_model(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("before_model_request")
    async def _(ctx: HookContext[NoState], options: RequestOptions) -> RequestOptions:
        ctx.actions.stop("no model calls")
        return options


async def test_a_stop_takes_effect_before_the_model_call() -> None:
    h = harness((agent(),), two_steps(), extensions=[no_model])

    outcome = await h.loop.run()

    assert outcome.status == "stopped"
    assert h.model.requests == []


@extension(id="t.late_judge", api_version=1)
def late_judge(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_run_end")
    async def _(ctx: HookContext[NoState]) -> None:
        async def judge() -> None:
            await asyncio.sleep(0.05)
            ctx.emit("verdict", {"ok": True})

        ctx.spawn(judge())


async def test_spawned_work_is_committed_before_the_terminal_event() -> None:
    h = harness((agent(),), two_steps(), extensions=[late_judge])

    await h.loop.run()

    assert [e.record.kind for e in h.store.events][-2:] == ["extension", "lifecycle"]


@extension(id="t.stuck", api_version=1, hook_timeout_s=0.05)
def stuck(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_run_end")
    async def _(ctx: HookContext[NoState]) -> None:
        ctx.spawn(asyncio.sleep(10))


async def test_spawned_work_still_running_after_its_timeout_fails_the_run() -> None:
    h = harness((agent(),), two_steps(), extensions=[stuck])

    with pytest.raises(ExtensionError, match=re.escape("still running 0.05s after the run ended")):
        await h.loop.run()

    last = h.store.events[-1]
    assert isinstance(last.record, LifecycleRecord)
    assert (last.record.status, last.record.hook, last.extension) == ("failed", "spawn", "t.stuck")


lifecycle_seen: list[str] = []


@extension(id="t.lifecycle", api_version=1)
def lifecycle(ext: ExtensionAPI[NoConfig, NoState]) -> None:
    @ext.on("on_event")
    async def _(ctx: HookContext[NoState], event: CommittedEvent) -> None:
        if isinstance(event.record, LifecycleRecord):
            lifecycle_seen.append(event.record.status)


async def test_observers_see_the_terminal_event() -> None:
    lifecycle_seen.clear()
    h = harness((agent(),), two_steps(), extensions=[lifecycle])

    await h.loop.run()

    assert lifecycle_seen == ["started", "finished"]


async def test_observers_get_no_events_after_a_failure() -> None:
    lifecycle_seen.clear()
    h = harness((agent(),), two_steps(), extensions=[lifecycle, broken])

    with pytest.raises(ExtensionError):
        await h.loop.run()

    assert lifecycle_seen == ["started"]
