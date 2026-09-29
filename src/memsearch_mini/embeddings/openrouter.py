"""OpenRouter embedding provider — the OpenAI embeddings API behind OpenRouter's gateway.

Requires the ``openai`` extra (OpenRouter has no SDK of its own here).
Environment variables:
    OPENROUTER_API_KEY — required

Models carry their vendor prefix, e.g. ``openai/text-embedding-3-small``. ``OPENAI_API_KEY``
and ``OPENAI_BASE_URL`` are never read: an OpenAI key must not travel to another vendor, and
the endpoint is fixed unless ``embedding.base_url`` overrides it.
"""

from __future__ import annotations

import os

from .openai import OpenAIEmbedding

BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterEmbedding(OpenAIEmbedding):
    """OpenRouter embedding provider (OpenAI-compatible endpoint)."""

    def __init__(
        self,
        model: str = "openai/text-embedding-3-small",
        *,
        batch_size: int = 0,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        # The config file's key wins; the environment is the fallback, as for every provider.
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            # Without an explicit key the openai SDK would fall back to OPENAI_API_KEY.
            raise ValueError("OPENROUTER_API_KEY not set (or set embedding.api_key)")
        super().__init__(model, batch_size=batch_size, base_url=base_url or BASE_URL, api_key=key)
