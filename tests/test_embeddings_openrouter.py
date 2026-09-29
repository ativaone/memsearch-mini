"""The openrouter embedding provider: OpenRouter's endpoint and key, never OpenAI's."""

import pytest

pytest.importorskip("openai")

from memsearch_mini.embeddings import get_provider
from memsearch_mini.embeddings.openrouter import BASE_URL


@pytest.fixture(autouse=True)
def _openai_env(monkeypatch):
    # Both set, so any leak into the OpenRouter client would show.
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://openai-gateway.internal/v1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def test_defaults_to_openrouter_endpoint_and_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")

    provider = get_provider("openrouter")

    assert provider.model_name == "openai/text-embedding-3-small"
    assert provider.dimension == 1536  # known model: no trial embed over the network
    assert str(provider._client.base_url).rstrip("/") == BASE_URL
    assert provider._client.api_key == "router-key"


def test_config_key_wins_over_the_environment_and_base_url_overrides(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")

    provider = get_provider("openrouter", api_key="literal", base_url="http://proxy.internal/v1")

    assert provider._client.api_key == "literal"
    assert str(provider._client.base_url).rstrip("/") == "http://proxy.internal/v1"


def test_the_environment_key_is_the_fallback(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")

    assert get_provider("openrouter")._client.api_key == "router-key"


def test_refuses_to_fall_back_to_the_openai_key():
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        get_provider("openrouter")
