"""Providers must honour the per-item index of an embeddings response.

The OpenAI-shaped APIs (OpenAI itself, Mistral, Jina) number each item instead
of guaranteeing order, and a compatible server behind a custom base URL may use
that freedom; a misordered batch would silently attach every embedding to the
wrong chunk.  The fakes below bypass ``__init__`` so no SDK or network is needed.
"""

from types import SimpleNamespace

import pytest

TEXTS = ["first", "second", "third"]
VECTORS = [[1.0], [2.0], [3.0]]


def _scrambled(items: list) -> list:
    return [items[2], items[0], items[1]]


@pytest.mark.asyncio
async def test_openai_reorders_by_the_response_index_and_tolerates_absence():
    from memsearch_mini.embeddings.openai import OpenAIEmbedding

    scrambled = _scrambled([SimpleNamespace(index=i, embedding=v) for i, v in enumerate(VECTORS)])
    unnumbered = [SimpleNamespace(embedding=v) for v in VECTORS]
    responses = iter([scrambled, unnumbered])

    class Embeddings:
        async def create(self, **_kwargs):
            return SimpleNamespace(data=next(responses))

    provider = object.__new__(OpenAIEmbedding)
    provider._model = "m"
    provider._client = SimpleNamespace(embeddings=Embeddings())
    assert await provider._embed_batch(TEXTS) == VECTORS
    # openai-python builds response models leniently: a server that omits index
    # must keep its wire order (stable sort), not raise AttributeError.
    assert await provider._embed_batch(TEXTS) == VECTORS


@pytest.mark.asyncio
async def test_mistral_reorders_by_the_response_index_and_tolerates_none():
    from memsearch_mini.embeddings.mistral import MistralEmbedding

    scrambled = _scrambled([SimpleNamespace(index=i, embedding=v) for i, v in enumerate(VECTORS)])
    unnumbered = [SimpleNamespace(index=None, embedding=v) for v in VECTORS]
    responses = iter([scrambled, unnumbered])

    class Embeddings:
        async def create_async(self, **_kwargs):
            return SimpleNamespace(data=next(responses))

    provider = object.__new__(MistralEmbedding)
    provider._model = "m"
    provider._client = SimpleNamespace(embeddings=Embeddings())
    assert await provider._embed_batch(TEXTS) == VECTORS
    # index typed optional in the SDK: all-None keeps the wire order (stable sort).
    assert await provider._embed_batch(TEXTS) == VECTORS


@pytest.mark.asyncio
async def test_jina_reorders_by_the_response_index_and_tolerates_absence():
    from memsearch_mini.embeddings.jina import JinaEmbedding

    scrambled = {"data": _scrambled([{"index": i, "embedding": v} for i, v in enumerate(VECTORS)])}
    unnumbered = {"data": [{"embedding": v} for v in VECTORS]}
    nulled = {"data": [{"index": None, "embedding": v} for v in VECTORS]}
    responses = iter([scrambled, unnumbered, nulled])

    class Client:
        async def post(self, _url, json=None, headers=None):
            payload = next(responses)
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    provider = object.__new__(JinaEmbedding)
    provider._model = "m"
    provider._task = ""
    provider._dimensions = 0
    provider._api_key = "k"
    provider._client = Client()
    assert await provider._embed_batch(TEXTS) == VECTORS
    # A payload with no index fields keeps its wire order (stable sort).
    assert await provider._embed_batch(TEXTS) == VECTORS
    # An explicit null index counts as absent, not as a sort key.
    assert await provider._embed_batch(TEXTS) == VECTORS
