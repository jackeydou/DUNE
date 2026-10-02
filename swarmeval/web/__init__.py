"""`web_request`'s client: the only way an agent reaches the internet. See
docs/services/orchestrator.md#web_request."""

from swarmeval.web.addresses import is_public
from swarmeval.web.client import HttpWebClient, Resolver, WebLimits, system_resolve

__all__ = ["HttpWebClient", "Resolver", "WebLimits", "is_public", "system_resolve"]
