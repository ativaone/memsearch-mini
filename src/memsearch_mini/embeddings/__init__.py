"""Embedding providers — protocol, factory, and concrete implementations."""

from __future__ import annotations

import inspect
import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Minimal interface every embedding backend must satisfy."""

    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    @property
    def batch_size(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


# Provider registry: name -> (module_path, class_name)
_PROVIDERS: dict[str, tuple[str, str]] = {
    "openai": ("memsearch_mini.embeddings.openai", "OpenAIEmbedding"),
    "google": ("memsearch_mini.embeddings.google", "GoogleEmbedding"),
    "voyage": ("memsearch_mini.embeddings.voyage", "VoyageEmbedding"),
    "jina": ("memsearch_mini.embeddings.jina", "JinaEmbedding"),
    "mistral": ("memsearch_mini.embeddings.mistral", "MistralEmbedding"),
    "ollama": ("memsearch_mini.embeddings.ollama", "OllamaEmbedding"),
    "local": ("memsearch_mini.embeddings.local", "LocalEmbedding"),
    "onnx": ("memsearch_mini.embeddings.onnx", "OnnxEmbedding"),
}

# Default model for each provider (mirrors the __init__ defaults in each class).
# Kept here so callers can resolve the effective model without importing heavy deps.
DEFAULT_MODELS: dict[str, str] = {
    "openai": "text-embedding-3-small",
    "google": "gemini-embedding-001",
    "voyage": "voyage-3-lite",
    "jina": "jina-embeddings-v4",
    "mistral": "mistral-embed",
    "ollama": "nomic-embed-text",
    "local": "all-MiniLM-L6-v2",
    "onnx": "gpahal/bge-m3-onnx-int8",
}

_INSTALL_HINTS: dict[str, str] = {
    name: f"run 'uv sync --extra {name}' in the plugin directory"
    for name in ("openai", "google", "voyage", "jina", "mistral", "ollama", "local", "onnx")
}


def get_provider(
    name: str = "openai",
    *,
    model: str | None = None,
    batch_size: int = 0,
    base_url: str | None = None,
    api_key: str | None = None,
) -> EmbeddingProvider:
    """Instantiate an embedding provider by name.

    *base_url* and *api_key* are forwarded only to the providers whose
    constructor actually accepts them (decided by signature introspection).  When
    a provider's SDK only reads the environment, *api_key* is placed in that
    variable with ``setdefault``, so a real environment variable always wins.
    """
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown embedding provider {name!r}. Available: {', '.join(sorted(_PROVIDERS))}")

    module_path, class_name = _PROVIDERS[name]
    try:
        import importlib

        mod = importlib.import_module(module_path)
    except ImportError as exc:
        hint = _INSTALL_HINTS.get(name, "")
        raise ImportError(f"Embedding provider {name!r} requires extra dependencies. Install with: {hint}") from exc

    cls = getattr(mod, class_name)
    accepted = inspect.signature(cls.__init__).parameters
    kwargs: dict = {}
    if model is not None:
        kwargs["model"] = model
    if batch_size > 0:
        kwargs["batch_size"] = batch_size
    if base_url and "base_url" in accepted:
        kwargs["base_url"] = base_url
    if api_key:
        from ..config import api_key_env_var

        env_var = api_key_env_var(name)
        if "api_key" in accepted:
            kwargs["api_key"] = api_key
        elif env_var:
            # The SDK only reads the environment; a real env var still wins.
            os.environ.setdefault(env_var, api_key)
    return cls(**kwargs)


__all__ = ["DEFAULT_MODELS", "EmbeddingProvider", "get_provider"]
