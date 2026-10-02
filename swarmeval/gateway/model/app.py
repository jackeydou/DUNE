"""The gateway's HTTP API: OpenAI Chat Completions, recorded (docs/services/model-gateway.md)."""

import hashlib
import hmac
import json
import logging
from typing import Annotated

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from swarmeval.gateway.model.client import UPSTREAM_ERROR_STATUS
from swarmeval.gateway.model.recorder import Attachments, NotAttachedError
from swarmeval.gateway.model.upstream import UnknownModelError, UpstreamError, Upstreams
from swarmeval.gateway.model.wire import (
    CALL_ID_HEADER,
    ChatRequest,
    ErrorBody,
    ErrorResponse,
)
from swarmeval.proto.swarmeval.modelgw.v1 import recorder_pb2 as pb

log = logging.getLogger(__name__)


def _error(status: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message))
    return JSONResponse(body.model_dump(), status_code=status)


def create_app(
    attachments: Attachments, upstreams: Upstreams, analysis_key: str | None = None
) -> FastAPI:
    """`analysis_key`, when set, is served without a run: see `GatewayConfig.analysis_key_env`."""
    app = FastAPI(title="swarmeval model-gateway", docs_url=None, redoc_url=None)

    @app.get("/v1/models")
    async def models() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "owned_by": "swarmeval"} for m in upstreams.models()
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        call_id: Annotated[str | None, Header(alias=CALL_ID_HEADER)] = None,
    ) -> Response:
        key = (authorization or "").removeprefix("Bearer ").strip()
        if analysis_key is not None and key and hmac.compare_digest(key, analysis_key):
            return await _analysis_call(request, call_id, upstreams)
        found = attachments.lookup(key) if key else None
        if found is None:
            # Keys exist only while their run's stream is attached, so an unknown key and a
            # detached run look the same here. Either way the backend is never called.
            return _error(
                503,
                "run_not_attached",
                "No attached run holds this key. The run's worker must attach before calling.",
            )
        attachment, caller = found
        if not call_id:
            return _error(400, "missing_call_id", f"Send the `{CALL_ID_HEADER}` header.")
        body = await request.body()
        try:
            chat = ChatRequest.model_validate_json(body)
        except ValidationError as err:
            return _error(400, "invalid_request", str(err))
        try:
            result = await upstreams.complete(chat)
        except UnknownModelError as err:
            return _error(404, "model_not_found", str(err))
        except UpstreamError as err:
            log.warning("run %s call %s: %s", attachment.run_id, call_id, err)
            return _error(UPSTREAM_ERROR_STATUS, "upstream_error", str(err))
        response_json = result.response.model_dump_json()
        upstream = pb.Upstream(
            backend=result.backend,
            model=result.route.upstream_model,
            served_model=result.served_model,
            reasoning_passback=result.passback,
            weights_hash=result.weights_hash or "",
            system_fingerprint=result.response.system_fingerprint or "",
            sampling_json=json.dumps(result.sampling, separators=(",", ":")),
            reasoning_visibility="full",
        )
        record = pb.CallRecord(
            call_id=call_id,
            caller=caller,
            request_sha256=hashlib.sha256(body).hexdigest(),
            response_json=response_json,
            upstream_response_json=result.raw_json,
            upstream=upstream,
            latency_seconds=result.latency_s,
            attempts=result.attempts,
        )
        try:
            await attachments.record(attachment, record)
        except NotAttachedError as err:
            # The response is discarded: the agent never sees a call that was not committed.
            log.warning("run %s call %s: %s", attachment.run_id, call_id, err)
            return _error(503, "run_not_attached", str(err))
        return Response(content=response_json, media_type="application/json")

    return app


async def _analysis_call(request: Request, call_id: str | None, upstreams: Upstreams) -> Response:
    if not call_id:
        return _error(400, "missing_call_id", f"Send the `{CALL_ID_HEADER}` header.")
    try:
        chat = ChatRequest.model_validate_json(await request.body())
    except ValidationError as err:
        return _error(400, "invalid_request", str(err))
    try:
        result = await upstreams.complete(chat)
    except UnknownModelError as err:
        return _error(404, "model_not_found", str(err))
    except UpstreamError as err:
        log.warning("analysis call %s: %s", call_id, err)
        return _error(UPSTREAM_ERROR_STATUS, "upstream_error", str(err))
    return Response(content=result.response.model_dump_json(), media_type="application/json")
