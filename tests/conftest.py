"""Shared fixtures.

Every test runs with ``HOME``, ``MEMSEARCH_CONFIG`` and ``TMPDIR`` pointed inside
``tmp_path``, so no test can read or write the developer's real ``~/.memsearch``.
The store-related environment switches are cleared for the same reason: a value
left over in the shell must never change what the suite exercises.
"""

from __future__ import annotations

import hashlib

import pytest

_ENV_SWITCHES = ("MEMSEARCH_NO_FTS", "MEMSEARCH_MAX_FILE_MB", "MEMSEARCH_DISABLE", "MEMSEARCH_DIR")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point every ``~`` lookup at a throwaway directory."""
    home = tmp_path / "home"
    (home / ".memsearch").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("MEMSEARCH_CONFIG", str(home / ".memsearch" / "config.toml"))
    for name in _ENV_SWITCHES:
        monkeypatch.delenv(name, raising=False)
    return home


class FakeEmbedder:
    """Deterministic 8-dimension embedder: no network, no model download.

    Vectors come from the SHA-256 of the text, so identical text always embeds
    identically and unrelated text lands far away on the unit sphere.
    """

    def __init__(self, *, dim: int = 8, batch_size: int = 4, model: str = "fake-embed") -> None:
        self._dim = dim
        self._batch_size = batch_size
        self._model = model
        self.calls: list[list[str]] = []

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def batch_size(self) -> int:
        return self._batch_size

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vector(text) for text in texts]

    def vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [digest[i % len(digest)] / 255.0 for i in range(self._dim)]
        norm = sum(value * value for value in raw) ** 0.5 or 1.0
        return [value / norm for value in raw]

    @property
    def call_sizes(self) -> list[int]:
        return [len(call) for call in self.calls]


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def make_embedder():
    """Factory for extra fake embedders (other dimensions, batch sizes, models)."""
    return FakeEmbedder
