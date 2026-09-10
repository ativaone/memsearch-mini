"""Tests for embedding batch-size handling.

The ``batched_embed`` helper is exercised directly, then end to end through
``index_paths`` with a fake provider, so no network call is ever made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memsearch_mini.embeddings.utils import batched_embed
from memsearch_mini.store import Store, index_paths

# -- batched_embed ---------------------------------------------------------------


class _Recorder:
    """Records the size of every batch it is handed."""

    def __init__(self, dim: int = 4) -> None:
        self.call_sizes: list[int] = []
        self._dim = dim

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.call_sizes.append(len(texts))
        return [[0.0] * self._dim for _ in texts]


async def test_batched_embed_splits():
    rec = _Recorder()
    assert len(await batched_embed(list("abcdefghij"), rec, batch_size=4)) == 10
    assert rec.call_sizes == [4, 4, 2]


async def test_batched_embed_single_batch():
    rec = _Recorder()
    assert len(await batched_embed(list("abc"), rec, batch_size=4)) == 3
    assert rec.call_sizes == [3]  # under the limit, not split


async def test_batched_embed_exact():
    rec = _Recorder()
    assert len(await batched_embed(list("abcd"), rec, batch_size=4)) == 4
    assert rec.call_sizes == [4]


async def test_batched_embed_empty():
    rec = _Recorder()
    assert await batched_embed([], rec, batch_size=4) == []
    assert rec.call_sizes == []


async def test_batched_embed_invalid_batch_size():
    with pytest.raises(ValueError, match="batch_size must be >= 1"):
        await batched_embed(["a"], _Recorder(), batch_size=0)


# -- index_paths ------------------------------------------------------------------


def _journal(tmp_path: Path, sections: int) -> Path:
    memory = tmp_path / "memory"
    memory.mkdir()
    body = "".join(f"## Section {i}\n\n- note number {i}\n\n" for i in range(sections))
    (memory / "log.md").write_text(body, encoding="utf-8")
    return memory


def _store(tmp_path: Path, embedder) -> Store:
    return Store.open(
        tmp_path / "index.db",
        provider="fake",
        model=embedder.model_name,
        dimension=embedder.dimension,
        allow_rebuild=True,
    )


@pytest.mark.parametrize(("sections", "batch_size", "expected"), [(10, 4, [4, 4, 2]), (3, 4, [3]), (4, 4, [4])])
async def test_index_paths_embeds_in_provider_sized_batches(tmp_path, make_embedder, sections, batch_size, expected):
    embedder = make_embedder(batch_size=batch_size)
    with _store(tmp_path, embedder) as store:
        result = await index_paths(store, embedder, [_journal(tmp_path, sections)])
    assert result.indexed_chunks == sections
    assert embedder.call_sizes == expected


async def test_index_paths_upserts_once_per_embedding_batch(tmp_path, make_embedder, monkeypatch):
    embedder = make_embedder(batch_size=4)
    upserts: list[int] = []
    original = Store.upsert

    def spy(self, records):
        upserts.append(len(records))
        return original(self, records)

    monkeypatch.setattr(Store, "upsert", spy)
    with _store(tmp_path, embedder) as store:
        result = await index_paths(store, embedder, [_journal(tmp_path, 10)])
        assert store.count() == 10
    assert result.indexed_chunks == 10
    assert upserts == [4, 4, 2]
    assert embedder.call_sizes == [4, 4, 2]


async def test_a_provider_with_no_batch_limit_embeds_in_one_call(tmp_path, make_embedder):
    embedder = make_embedder(batch_size=0)
    with _store(tmp_path, embedder) as store:
        result = await index_paths(store, embedder, [_journal(tmp_path, 7)])
    assert result.indexed_chunks == 7
    assert embedder.call_sizes == [7]


async def test_indexing_an_empty_directory_embeds_nothing(tmp_path, fake_embedder):
    empty = tmp_path / "memory"
    empty.mkdir()
    with _store(tmp_path, fake_embedder) as store:
        result = await index_paths(store, fake_embedder, [empty])
    assert (result.indexed_chunks, result.total_files) == (0, 0)
    assert fake_embedder.calls == []


async def test_a_failing_provider_isolates_the_file_it_broke_on(tmp_path, make_embedder):
    memory = tmp_path / "memory"
    memory.mkdir()
    for name in ("aaa.md", "bbb.md", "ccc.md"):
        (memory / name).write_text(f"# {name}\n\n- a note from {name}\n", encoding="utf-8")

    embedder = make_embedder(batch_size=8)
    original_embed = embedder.embed

    async def embed(texts):
        if any("bbb.md" in text for text in texts):
            raise RuntimeError("provider rejected the request")
        return await original_embed(texts)

    embedder.embed = embed
    with _store(tmp_path, embedder) as store:
        result = await index_paths(store, embedder, [memory])
        assert store.count() == 2

    assert result.total_files == 3
    assert result.indexed_chunks == 2
    assert len(result.failed_files) == 1
    assert result.failed_files[0][0].endswith("bbb.md")
    assert result.failed_files[0][1] == "RuntimeError: provider rejected the request"
