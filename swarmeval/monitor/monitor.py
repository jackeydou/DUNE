"""`swarmeval.monitor`: runs detectors on every event as it commits (M2 spec decision 7).

Each hit is an `alert` parented to the event that triggered it; the configured action then
follows with the alert as its cause, so `trace` from the action leads through the alert to what
caused it. Detector state is part of the extension state, so a resumed or forked run goes on
with what the detectors remembered.
"""

from swarmeval.detect.detectors import Detector, Hit, RunTokens, build
from swarmeval.detect.view import view_of
from swarmeval.monitor.config import MONITOR, MonitorConfig, MonitorState
from swarmeval.runtime.extensions import ExtensionAPI, HookContext, extension
from swarmeval.runtime.records import CommittedEvent


@extension(id=MONITOR, api_version=1, config=MonitorConfig, state=MonitorState)
def setup(ext: ExtensionAPI[MonitorConfig, MonitorState]) -> None:
    cfg = ext.config
    built: list[Detector] = []
    """Built at the first event: canary detectors need the run's tokens, which setup has not."""

    @ext.on("on_event")
    async def watch(ctx: HookContext[MonitorState], event: CommittedEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        state = ctx.state
        if not built:
            tokens = RunTokens(ctx.run.canaries, ctx.run.sandbox_canaries)
            built.extend(build(d, tokens) for d in cfg.detectors)
            for detector, saved in zip(built, state.detectors, strict=False):
                detector.restore(saved)
        view = view_of(event, {a.id: a.sandbox_id for a in ctx.run.agents})
        hits = [hit for detector in built for hit in detector.observe(view)]
        state.detectors = [d.state() for d in built]
        for hit in hits:
            state.hits += 1
            _act(ctx, cfg, hit, event)


def _act(
    ctx: HookContext[MonitorState], cfg: MonitorConfig, hit: Hit, event: CommittedEvent
) -> None:
    alert = ctx.actions.alert(
        f"{hit.detector}: {hit.detail}", severity=cfg.severity, event_ids=hit.event_ids
    )
    state = ctx.state
    if cfg.on_hit == "alert" or (cfg.max_actions is not None and state.actions >= cfg.max_actions):
        return
    reason = f"{hit.detector} hit on event {event.event_id}"
    match cfg.on_hit:
        case "pause":
            ctx.actions.pause(reason, cause=alert)
        case "stop":
            ctx.actions.stop(reason, cause=alert)
        case "inject":
            assert cfg.inject is not None, "checked by MonitorConfig"
            agent = cfg.inject.agent or event.agent_id
            if agent is None:
                return
            ctx.actions.inject(agent, cfg.inject.content, cause=alert)
    state.actions += 1
