"""Calls with the analysis key: served without a run, never recorded as run evidence."""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx2
import pytest

from swarmeval.gateway.model.app import create_app
from swarmeval.gateway.model.config import GatewayConfig, GatewayConfigError
from swarmeval.gateway.model.recorder import Attachments
from swarmeval.gateway.model.upstream import Upstreams
from swarmeval.gateway.model.wire import CALL_ID_HEADER
from tests.gateway.mock_backend import MockBackend, completion
from tests.gateway.test_gateway import CONFIG

KEY = "a" * 64
BODY = {"model": "qwen-test", "messages": [{"role": "user", "content": "judge this"}]}


@dataclass
class AnalysisRig:
    backend: MockBackend
    http: httpx2.AsyncClient


async def rig_with(analysis_key: str | None) -> AsyncIterator[AnalysisRig]:
    backend = MockBackend()
    upstreams = Upstreams(CONFIG, transports={"mock": httpx2.ASGITransport(app=backend.app())})
    app = create_app(Attachments(), upstreams, analysis_key)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://gateway"
    ) as http:
        yield AnalysisRig(backend, http)
    await upstreams.close()


@pytest.fixture
async def rig() -> AsyncIterator[AnalysisRig]:
    async for r in rig_with(KEY):
        yield r


@pytest.fixture
async def rig_without_key() -> AsyncIterator[AnalysisRig]:
    async for r in rig_with(None):
        yield r


def headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", CALL_ID_HEADER: "judge-1"}


async def test_the_analysis_key_is_answered_without_a_run(rig: AnalysisRig) -> None:
    rig.backend.reply(completion("verdict"))

    reply = await rig.http.post("/v1/chat/completions", json=BODY, headers=headers(KEY))

    assert reply.status_code == 200
    assert reply.json()["choices"][0]["message"]["content"] == "verdict"
    assert rig.backend.requests[0]["model"] == "Org/Upstream-Model"


async def test_any_other_key_still_needs_an_attached_run(rig: AnalysisRig) -> None:
    reply = await rig.http.post("/v1/chat/completions", json=BODY, headers=headers("b" * 64))

    assert reply.status_code == 503
    assert reply.json()["error"]["code"] == "run_not_attached"
    assert rig.backend.requests == []


async def test_without_an_analysis_key_configured_none_is_served(
    rig_without_key: AnalysisRig,
) -> None:
    reply = await rig_without_key.http.post("/v1/chat/completions", json=BODY, headers=headers(KEY))

    assert reply.status_code == 503
    assert rig_without_key.backend.requests == []


async def test_analysis_calls_need_a_call_id_and_a_valid_body(rig: AnalysisRig) -> None:
    no_id = await rig.http.post(
        "/v1/chat/completions", json=BODY, headers={"Authorization": f"Bearer {KEY}"}
    )
    bad = await rig.http.post(
        "/v1/chat/completions", json={**BODY, "stream": True}, headers=headers(KEY)
    )

    assert no_id.json()["error"]["code"] == "missing_call_id"
    assert bad.json()["error"]["code"] == "invalid_request"
    assert rig.backend.requests == []


def test_the_analysis_key_must_be_set_and_long(monkeypatch: pytest.MonkeyPatch) -> None:
    config = CONFIG.model_copy(update={"analysis_key_env": "TEST_ANALYSIS_KEY"})
    monkeypatch.delenv("TEST_ANALYSIS_KEY", raising=False)
    with pytest.raises(GatewayConfigError, match="TEST_ANALYSIS_KEY"):
        config.analysis_key()
    monkeypatch.setenv("TEST_ANALYSIS_KEY", "short")
    with pytest.raises(GatewayConfigError, match="at least 32"):
        config.analysis_key()
    monkeypatch.setenv("TEST_ANALYSIS_KEY", KEY)
    assert config.analysis_key() == KEY
    assert GatewayConfig.model_validate(CONFIG.model_dump()).analysis_key() is None
