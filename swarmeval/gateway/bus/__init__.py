"""The Message Bus: channels between a run's agents (docs/services/orchestrator.md#message-bus)."""

from swarmeval.gateway.bus.bus import ChannelSpec, MessageBus, SendArgs

__all__ = ["ChannelSpec", "MessageBus", "SendArgs"]
