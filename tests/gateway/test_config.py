from pathlib import Path

import pytest

from swarmeval.gateway.model.config import GatewayConfig, GatewayConfigError


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "gateway.yaml"
    path.write_text(text)
    return path


def test_a_route_to_an_unknown_backend_is_refused(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "backends: {vllm: {base_url: 'http://vllm/v1'}}\n"
        "models: {qwen: {backend: sglang, upstream_model: Qwen/Qwen3}}\n",
    )

    with pytest.raises(GatewayConfigError, match="model `qwen` routes to unknown backend `sglang`"):
        GatewayConfig.load(path)


def test_a_backend_key_comes_from_the_named_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(
        tmp_path,
        "backends: {hosted: {base_url: 'https://api/v1', api_key_env: HOSTED_KEY}}\nmodels: {}\n",
    )
    backend = GatewayConfig.load(path).backends["hosted"]

    monkeypatch.delenv("HOSTED_KEY", raising=False)
    with pytest.raises(GatewayConfigError, match="`HOSTED_KEY` is not set"):
        backend.api_key()
    monkeypatch.setenv("HOSTED_KEY", "sk-test")
    assert backend.api_key() == "sk-test"
