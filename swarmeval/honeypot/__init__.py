"""Canaries: generation, placement files, and matching (docs/services/orchestrator.md)."""

from swarmeval.honeypot.canary import (
    PlacedCanary,
    Sighting,
    canary_key,
    delivered,
    place,
    place_sandboxes,
    sightings,
)
from swarmeval.honeypot.decode import Found, View, find_tokens, views, xor_layer

__all__ = [
    "Found",
    "PlacedCanary",
    "Sighting",
    "View",
    "canary_key",
    "delivered",
    "find_tokens",
    "place",
    "place_sandboxes",
    "sightings",
    "views",
    "xor_layer",
]
