"""Canaries: generation, placement files, and matching (docs/services/orchestrator.md)."""

from swarmeval.honeypot.canary import PlacedCanary, Sighting, place, sightings
from swarmeval.honeypot.decode import Found, find_tokens

__all__ = ["Found", "PlacedCanary", "Sighting", "find_tokens", "place", "sightings"]
