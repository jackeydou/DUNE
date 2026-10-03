import grpc
import pytest

from swarmeval.gateway.model.client import UPSTREAM_ERROR_STATUS, ModelGatewayError
from swarmeval.sandbox import SandboxdError
from swarmeval.worker.run import service_failure


@pytest.mark.parametrize(
    ("err", "status"),
    [
        (ModelGatewayError("context too long", UPSTREAM_ERROR_STATUS), "failed"),
        (ModelGatewayError("model_not_found", 404), "failed"),
        (ModelGatewayError("invalid_request", 400), "failed"),
        (ModelGatewayError("internal server error", 500), "interrupted"),
        (ModelGatewayError("run_not_attached", 503), "interrupted"),
        (ModelGatewayError("model-gateway is unreachable"), "interrupted"),
        (ModelGatewayError("the stream failed earlier"), "interrupted"),
        (SandboxdError("sandboxd unavailable", grpc.StatusCode.UNAVAILABLE), "interrupted"),
        (SandboxdError("sandboxd forgot the run", grpc.StatusCode.NOT_FOUND), "interrupted"),
        (SandboxdError("internal", grpc.StatusCode.INTERNAL), "interrupted"),
        (SandboxdError("blob hash mismatch"), "interrupted"),
        (SandboxdError("hostname must be [a-z0-9-]", grpc.StatusCode.INVALID_ARGUMENT), "failed"),
    ],
)
def test_a_backend_refusal_fails_the_run_and_an_outage_interrupts_it(
    err: SandboxdError | ModelGatewayError, status: str
) -> None:
    outcome = service_failure(err)

    assert (outcome.status, outcome.error) == (status, str(err))
