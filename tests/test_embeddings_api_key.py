"""``embedding.api_key`` reaches every keyed provider explicitly and never touches the environment."""

import inspect
import os

import pytest

from memsearch_mini.config import api_key_env_var
from memsearch_mini.embeddings import _PROVIDERS, get_provider


@pytest.mark.parametrize("name", sorted(n for n in _PROVIDERS if api_key_env_var(n)))
def test_every_keyed_provider_accepts_an_explicit_key(name):
    """Without the parameter the key could only travel through os.environ, where the env would win."""
    module_path, class_name = _PROVIDERS[name]
    module = pytest.importorskip(module_path, exc_type=ImportError)
    assert "api_key" in inspect.signature(getattr(module, class_name).__init__).parameters


def test_the_literal_key_is_not_written_into_the_environment(monkeypatch):
    captured = {}

    class Fake:
        def __init__(self, *, api_key=None):
            captured["api_key"] = api_key

    monkeypatch.setitem(_PROVIDERS, "fake", ("types", "SimpleNamespace"))
    import types

    monkeypatch.setattr(types, "SimpleNamespace", Fake)
    environ_before = dict(os.environ)

    get_provider("fake", api_key="literal-key")

    assert captured == {"api_key": "literal-key"}
    assert dict(os.environ) == environ_before
