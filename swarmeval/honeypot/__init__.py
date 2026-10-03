"""Canaries: generation, placement files, and matching (docs/services/orchestrator.md)."""

from swarmeval.honeypot.canary import PlacedCanary, Sighting, place, sightings
from swarmeval.honeypot.decode import Found, View, find_tokens, views, xor_layer

__all__ = [
    "Found",
    "PlacedCanary",
    "Sighting",
    "View",
    "find_tokens",
    "place",
    "sightings",
    "views",
    "xor_layer",
]
