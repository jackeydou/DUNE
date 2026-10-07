"""A model catalog for a Control API with no model-gateway behind it."""

from swarmeval.control.models import GatewayUnavailable
from swarmeval.proto.swarmeval.control.v1 import control_pb2 as pb


class StaticModels:
    def __init__(self, *names: str) -> None:
        self.names = tuple(sorted(names))
        self.unavailable = False
        """Set to answer as an unreachable model-gateway would."""

    async def served(self) -> tuple[str, ...]:
        if self.unavailable:
            raise GatewayUnavailable("model-gateway at http://gw did not list its models: down.")
        return self.names


SERVED = ("m1", "m2", "m3")


def chosen(*names: str, slot: str = "default") -> dict[str, pb.ModelChoice]:
    """`SubmitRunsRequest.models` choosing `names` (default `m1`) for one slot."""
    return {slot: pb.ModelChoice(names=list(names or ("m1",)))}
