"""Unit tests for the Google embedding provider."""

from __future__ import annotations

import importlib
import sys
import types


def _install_fake_google_genai(monkeypatch, *, record: dict):
    google_module = types.ModuleType("google")
    genai_module = types.ModuleType("google.genai")

    class FakeClient:
        def __init__(self, *, vertexai: bool = False, api_key: str | None = None):
            record["vertexai"] = vertexai
            record["api_key"] = api_key
            self.models = types.SimpleNamespace(embed_content=self.embed_content)

        def embed_content(self, *, model: str, contents: list[str]):
            record["model"] = model
            record["contents"] = contents
            return types.SimpleNamespace(embeddings=[types.SimpleNamespace(values=[0.1, 0.2, 0.3])])

    genai_module.Client = FakeClient
    google_module.genai = genai_module

    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.genai", genai_module)


def _load_google_embedding_module():
    sys.modules.pop("memsearch_mini.embeddings.google", None)
    return importlib.import_module("memsearch_mini.embeddings.google")


def test_google_embedding_uses_vertex_ai_when_env_var_is_true(monkeypatch):
    record: dict = {}
    _install_fake_google_genai(monkeypatch, record=record)
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")

    GoogleEmbedding = _load_google_embedding_module().GoogleEmbedding

    provider = GoogleEmbedding(model="custom-vertex-model")

    assert record["vertexai"] is True
    assert provider.dimension == 3


def test_google_embedding_defaults_to_api_key_mode(monkeypatch):
    record: dict = {}
    _install_fake_google_genai(monkeypatch, record=record)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)

    GoogleEmbedding = _load_google_embedding_module().GoogleEmbedding

    provider = GoogleEmbedding(model="custom-api-key-model")

    assert record["vertexai"] is False
    assert provider.dimension == 3


def test_google_embedding_passes_the_config_key_over_the_environment(monkeypatch):
    record: dict = {}
    _install_fake_google_genai(monkeypatch, record=record)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "env-key")

    _load_google_embedding_module().GoogleEmbedding(model="m", api_key="literal-key")

    assert record["api_key"] == "literal-key"


def test_google_embedding_never_sends_the_key_to_vertex_ai(monkeypatch):
    record: dict = {}
    _install_fake_google_genai(monkeypatch, record=record)
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")

    _load_google_embedding_module().GoogleEmbedding(model="m", api_key="literal-key")

    assert (record["vertexai"], record["api_key"]) == (True, None)
