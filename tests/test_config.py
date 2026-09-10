"""Tests for the single-file TOML configuration layer."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from memsearch import config as cfg_mod
from memsearch.config import (
    DEFAULT_SUMMARIZE_MODELS,
    Config,
    api_key_env_var,
    coerce,
    config_path,
    get_value,
    load,
    load_raw,
    missing_api_key,
    save,
    set_value,
    to_dict,
)

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _read(path: Path) -> dict:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


# -- location --------------------------------------------------------------------


def test_config_path_follows_the_environment_override(tmp_path, monkeypatch):
    override = tmp_path / "elsewhere" / "config.toml"
    monkeypatch.setenv("MEMSEARCH_CONFIG", str(override))
    assert config_path() == override


def test_config_path_defaults_under_the_home_directory(monkeypatch, isolated_home):
    monkeypatch.delenv("MEMSEARCH_CONFIG", raising=False)
    assert config_path() == isolated_home / ".memsearch" / "config.toml"


# -- defaults and loading --------------------------------------------------------


def test_defaults_match_the_documented_configuration():
    cfg = Config()
    assert (cfg.embedding.provider, cfg.embedding.model) == ("onnx", "")
    assert (cfg.embedding.api_key, cfg.embedding.base_url, cfg.embedding.batch_size) == ("", "", 0)
    assert (cfg.chunking.max_chunk_size, cfg.chunking.overlap_lines) == (1500, 2)
    assert (cfg.claude.summarize_enabled, cfg.claude.summarize_model) == (True, "haiku")
    assert (cfg.codex.summarize_enabled, cfg.codex.summarize_model) == (True, "gpt-5.1-codex-mini")
    assert cfg.prompts.summarize == ""


def test_load_returns_defaults_when_no_file_exists(tmp_path):
    missing = tmp_path / "absent.toml"
    assert load_raw(missing) == {}
    assert to_dict(load(missing)) == to_dict(Config())


def test_load_overlays_only_the_keys_the_file_defines(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[embedding]\nprovider = "openai"\nbatch_size = 16\n', encoding="utf-8")
    cfg = load(path)
    assert (cfg.embedding.provider, cfg.embedding.batch_size) == ("openai", 16)
    assert cfg.embedding.model == ""  # untouched default
    assert cfg.chunking.max_chunk_size == 1500


def test_load_ignores_unknown_sections_and_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[milvus]\nuri = "x"\n\n[embedding]\nnonsense = 1\nprovider = "jina"\n', encoding="utf-8")
    cfg = load(path)
    assert cfg.embedding.provider == "jina"
    assert not hasattr(cfg, "milvus")
    assert not hasattr(cfg.embedding, "nonsense")


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "nested" / "config.toml"
    save({"embedding": {"provider": "voyage"}, "chunking": {"overlap_lines": 5}}, path)
    assert path.is_file()
    cfg = load(path)
    assert cfg.embedding.provider == "voyage"
    assert cfg.chunking.overlap_lines == 5


def test_load_uses_the_environment_path_when_none_is_given(tmp_path, monkeypatch):
    path = tmp_path / "env-config.toml"
    path.write_text('[embedding]\nprovider = "mistral"\n', encoding="utf-8")
    monkeypatch.setenv("MEMSEARCH_CONFIG", str(path))
    assert load().embedding.provider == "mistral"


# -- get / set -------------------------------------------------------------------


def test_get_value_reads_a_dotted_key():
    cfg = Config()
    cfg.embedding.provider = "google"
    assert get_value("embedding.provider", cfg) == "google"
    assert get_value("chunking.max_chunk_size", cfg) == 1500


@pytest.mark.parametrize("key", ["embedding", "embedding.nope", "nope.provider", "a.b.c", "", "."])
def test_unknown_keys_raise_key_error(key):
    with pytest.raises(KeyError):
        get_value(key, Config())


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("max_chunk_size", "800", 800),
        ("overlap_lines", "0", 0),
        ("batch_size", "32", 32),
        ("summarize_enabled", "true", True),
        ("summarize_enabled", "  On ", True),
        ("summarize_enabled", "1", True),
        ("summarize_enabled", "no", False),
        ("summarize_enabled", "OFF", False),
        ("provider", "openai", "openai"),
    ],
)
def test_coerce_converts_by_field_name(name, raw, expected):
    assert coerce(name, raw) == expected


def test_coerce_rejects_a_non_boolean_and_a_non_integer():
    with pytest.raises(ValueError, match="Expected a boolean"):
        coerce("summarize_enabled", "maybe")
    with pytest.raises(ValueError):
        coerce("batch_size", "many")


def test_set_value_persists_the_coerced_value(tmp_path):
    path = tmp_path / "config.toml"
    assert set_value("chunking.max_chunk_size", "900", path) == 900
    assert set_value("claude.summarize_enabled", "false", path) is False
    assert set_value("embedding.provider", "ollama", path) == "ollama"

    raw = _read(path)
    assert raw == {
        "chunking": {"max_chunk_size": 900},
        "claude": {"summarize_enabled": False},
        "embedding": {"provider": "ollama"},
    }
    cfg = load(path)
    assert (cfg.chunking.max_chunk_size, cfg.claude.summarize_enabled) == (900, False)
    assert cfg.chunking.overlap_lines == 2  # other defaults are not written out


def test_set_value_rejects_an_unknown_key_before_touching_the_file(tmp_path):
    path = tmp_path / "config.toml"
    with pytest.raises(KeyError):
        set_value("milvus.uri", "http://x", path)
    assert not path.exists()


def test_set_value_uses_the_environment_path_when_none_is_given(tmp_path, monkeypatch):
    path = tmp_path / "env-config.toml"
    monkeypatch.setenv("MEMSEARCH_CONFIG", str(path))
    set_value("embedding.provider", "jina")
    assert _read(path) == {"embedding": {"provider": "jina"}}


# -- derived values --------------------------------------------------------------


def test_effective_model_prefers_the_explicit_model():
    cfg = Config()
    cfg.embedding.model = "my-own-model"
    assert cfg.effective_model() == "my-own-model"


def test_effective_model_falls_back_to_the_provider_default():
    from memsearch.embeddings import DEFAULT_MODELS

    cfg = Config()
    assert cfg.effective_model() == DEFAULT_MODELS["onnx"]
    cfg.embedding.provider = "openai"
    assert cfg.effective_model() == DEFAULT_MODELS["openai"]
    cfg.embedding.provider = "not-a-provider"
    assert cfg.effective_model() == ""


def test_agent_returns_the_platform_section():
    cfg = Config()
    assert cfg.agent("claude").summarize_model == "haiku"
    assert cfg.agent("codex").summarize_model == "gpt-5.1-codex-mini"


@pytest.mark.parametrize("platform", ["claude", "codex"])
def test_agent_fills_an_empty_model_without_mutating_the_config(platform):
    cfg = Config()
    getattr(cfg, platform).summarize_model = ""
    assert cfg.agent(platform).summarize_model == DEFAULT_SUMMARIZE_MODELS[platform]
    assert getattr(cfg, platform).summarize_model == ""


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("openai", "OPENAI_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
        ("voyage", "VOYAGE_API_KEY"),
        ("jina", "JINA_API_KEY"),
        ("mistral", "MISTRAL_API_KEY"),
        ("onnx", ""),
        ("ollama", ""),
        ("local", ""),
        ("unknown", ""),
    ],
)
def test_api_key_env_var(provider, expected):
    assert api_key_env_var(provider) == expected


def test_missing_api_key_only_reports_providers_that_need_one(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config()
    assert missing_api_key(cfg) == ""  # onnx needs nothing

    cfg.embedding.provider = "openai"
    assert missing_api_key(cfg) == "OPENAI_API_KEY"

    cfg.embedding.api_key = "sk-literal"
    assert missing_api_key(cfg) == ""

    cfg.embedding.api_key = ""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert missing_api_key(cfg) == ""


def test_to_dict_is_a_plain_nested_mapping():
    data = to_dict(Config())
    assert data["embedding"]["provider"] == "onnx"
    assert data["claude"] == {"summarize_enabled": True, "summarize_model": "haiku"}
    assert set(data) == {"embedding", "chunking", "claude", "codex", "prompts"}


def test_the_module_exposes_no_legacy_milvus_settings():
    assert not hasattr(Config(), "milvus")
    assert "milvus" not in dir(cfg_mod)
