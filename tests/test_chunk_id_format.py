"""Tests for the chunk ID format."""

from __future__ import annotations

import hashlib
import inspect

from memsearch.chunker import Chunk, compute_chunk_id


def test_chunk_id_is_the_documented_sha256_prefix():
    expected = hashlib.sha256(b"markdown:test.md:1:5:hash123").hexdigest()[:16]
    assert compute_chunk_id("test.md", 1, 5, "hash123") == expected
    assert len(expected) == 16
    assert all(c in "0123456789abcdef" for c in expected)


def test_chunk_id_takes_no_model_argument():
    """The model lives in ``meta``; changing it rebuilds instead of duplicating."""
    assert list(inspect.signature(compute_chunk_id).parameters) == [
        "source",
        "start_line",
        "end_line",
        "content_hash",
    ]


def test_chunk_id_is_deterministic():
    assert compute_chunk_id("file.md", 1, 10, "abc") == compute_chunk_id("file.md", 1, 10, "abc")


def test_chunk_id_varies_with_every_component():
    base = compute_chunk_id("file.md", 1, 5, "hash")
    assert compute_chunk_id("other.md", 1, 5, "hash") != base
    assert compute_chunk_id("file.md", 2, 5, "hash") != base
    assert compute_chunk_id("file.md", 1, 6, "hash") != base
    assert compute_chunk_id("file.md", 1, 5, "hash2") != base


def test_chunk_id_matches_a_chunk_content_hash():
    chunk = Chunk(content="hello", source="a.md", heading="", heading_level=0, start_line=1, end_line=2)
    assert chunk.content_hash == hashlib.sha256(b"hello").hexdigest()[:16]
    assert compute_chunk_id(chunk.source, chunk.start_line, chunk.end_line, chunk.content_hash) == compute_chunk_id(
        "a.md", 1, 2, chunk.content_hash
    )
