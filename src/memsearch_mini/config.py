"""Configuration: a single TOML file at ~/.memsearch-mini/config.toml.

There is deliberately only one layer. Provider SDKs read their own environment
variables (OPENAI_API_KEY, ...); ``embedding.api_key`` is an optional literal.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import tomli_w

DEFAULT_SUMMARIZE_MODELS = {"claude": "haiku", "codex": "gpt-5.1-codex-mini"}

_API_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "voyage": "VOYAGE_API_KEY",
    "jina": "JINA_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}

# The summarizer's own providers: a closed set, because the plugin speaks their HTTP API itself.
_SUMMARIZE_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}
_SUMMARIZE_BASE_URL = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
}


def home_dir() -> Path:
    """Everything the plugin writes outside a project lives here (default ~/.memsearch-mini).

    ``MEMSEARCH_MINI_HOME`` relocates it; ``hooks/common.sh`` points the uv cache, the
    project venv and the model cache under it too, so uninstalling is one ``rm -rf``.
    """
    override = os.environ.get("MEMSEARCH_MINI_HOME")
    return Path(override).expanduser() if override else Path("~/.memsearch-mini").expanduser()


def config_path() -> Path:
    """Resolved at call time so tests can point MEMSEARCH_MINI_CONFIG at a temp file."""
    override = os.environ.get("MEMSEARCH_MINI_CONFIG")
    if override:
        return Path(override).expanduser()
    return home_dir() / "config.toml"


@dataclass
class EmbeddingConfig:
    provider: str = "onnx"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    batch_size: int = 0


@dataclass
class ChunkingConfig:
    max_chunk_size: int = 1500
    overlap_lines: int = 2
    min_chunk_size: int = 0  # > 0 merges consecutive small sections up to this size


@dataclass
class AgentConfig:
    summarize_enabled: bool = True
    summarize_model: str = ""


@dataclass
class SummarizeConfig:
    """How a turn is summarized, whichever host recorded it.

    ``harness`` (the default) runs the agent CLI that is already installed; ``api`` posts the
    turn to the provider's HTTP endpoint instead, which is the only way to summarize with a
    model the host cannot reach.
    """

    mode: str = "harness"  # harness | api
    provider: str = "openai"  # api mode only: openai | anthropic | google
    model: str = ""  # required in api mode; there is no sensible default across three vendors
    api_key: str = ""  # optional literal; a real environment variable always wins
    base_url: str = ""  # "" means the provider's public endpoint
    language: str = ""  # "" follows the user's own language; e.g. "pt-BR"


@dataclass
class PromptsConfig:
    summarize: str = ""


@dataclass
class MemoryConfig:
    filename_suffix: str = ""  # "" or "hostname": YYYY-MM-DD-<host>.md keeps synced folders from colliding


@dataclass
class Config:
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    claude: AgentConfig = field(default_factory=lambda: AgentConfig(summarize_model="haiku"))
    codex: AgentConfig = field(default_factory=lambda: AgentConfig(summarize_model="gpt-5.1-codex-mini"))
    summarize: SummarizeConfig = field(default_factory=SummarizeConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    def effective_model(self) -> str:
        if self.embedding.model:
            return self.embedding.model
        from .embeddings import DEFAULT_MODELS

        return DEFAULT_MODELS.get(self.embedding.provider, "")

    def agent(self, platform: str) -> AgentConfig:
        cfg = getattr(self, platform)
        if not cfg.summarize_model:
            cfg = dataclasses.replace(cfg, summarize_model=DEFAULT_SUMMARIZE_MODELS.get(platform, ""))
        return cfg


# Integer fields mapped to their floor: a max_chunk_size below 1 makes the chunker's hard
# split loop forever, and the other three are counts, so a negative value is meaningless.
_INT_FIELDS = {"max_chunk_size": 1, "overlap_lines": 0, "min_chunk_size": 0, "batch_size": 0}
_BOOL_FIELDS = {"summarize_enabled"}
# Keyed by field name, or by "section.field" when only that section's field is a closed set:
# [embedding] provider is open (any SDK the user installed), [summarize] provider is not.
_CHOICE_FIELDS = {
    "filename_suffix": ("", "hostname"),
    "summarize.mode": ("harness", "api"),
    "summarize.provider": ("openai", "anthropic", "google"),
}


def load_raw(path: Path | None = None) -> dict[str, Any]:
    """File contents as a plain dict; {} when the file does not exist."""
    path = path or config_path()
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def load(path: Path | None = None) -> Config:
    """Typed config: dataclass defaults overlaid with whatever the file defines."""
    raw = load_raw(path)
    cfg = Config()
    for section_name, section_raw in raw.items():
        section = getattr(cfg, section_name, None)
        if not dataclasses.is_dataclass(section) or not isinstance(section_raw, dict):
            continue
        for key, value in section_raw.items():
            if hasattr(section, key):
                setattr(section, key, value)
    return cfg


def save(data: dict[str, Any], path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)


def to_dict(cfg: Config) -> dict[str, Any]:
    return dataclasses.asdict(cfg)


def _split_key(key: str) -> tuple[str, str]:
    section, _, name = key.partition(".")
    if not section or not name or "." in name:
        raise KeyError(f"Config key must look like 'section.field', got {key!r}")
    defaults = Config()
    sec = getattr(defaults, section, None)
    if not dataclasses.is_dataclass(sec) or not hasattr(sec, name):
        raise KeyError(f"Unknown config key: {key}")
    return section, name


def get_value(key: str, cfg: Config | None = None) -> Any:
    section, name = _split_key(key)
    cfg = cfg or load()
    return getattr(getattr(cfg, section), name)


def coerce(name: str, value: str, *, section: str = "") -> Any:
    """Validate and convert one raw ``config set`` value; ``section`` narrows the choice lists."""
    if name in _INT_FIELDS:
        number, floor = int(value), _INT_FIELDS[name]
        if number < floor:
            raise ValueError(f"Expected an integer >= {floor} for {name}, got {value!r}")
        return number
    if name in _BOOL_FIELDS:
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"Expected a boolean for {name}, got {value!r}")
    choices = _CHOICE_FIELDS.get(f"{section}.{name}", _CHOICE_FIELDS.get(name))
    if choices is not None and value not in choices:
        raise ValueError(f"Expected one of {choices!r} for {name}, got {value!r}")
    return value


def set_value(key: str, value: str, path: Path | None = None) -> Any:
    """Coerce, persist and return the stored value."""
    section, name = _split_key(key)
    stored = coerce(name, value, section=section)
    raw = load_raw(path)
    if section in raw and not isinstance(raw[section], dict):
        raise ValueError(f"[{section}] in {path or config_path()} holds a value, not a section; "
                         f"fix the file by hand before setting {key}")  # fmt: skip
    raw.setdefault(section, {})[name] = stored
    save(raw, path)
    return stored


def api_key_env_var(provider: str) -> str:
    """Env var the provider needs, or "" when it needs none (onnx, ollama, local)."""
    return _API_KEY_ENV.get(provider, "")


def missing_api_key(cfg: Config) -> str:
    """Name of the required-but-unset env var, or "" when the provider is usable."""
    var = api_key_env_var(cfg.embedding.provider)
    if not var or cfg.embedding.api_key or os.environ.get(var):
        return ""
    return var


def summarize_problem(cfg: Config) -> str:
    """Reason ``[summarize]`` cannot summarize anything, or "" — harness mode is always usable.

    Only api mode can be misconfigured, and it is reported instead of enforced: a turn with an
    unusable summarizer is still journaled, with this reason where the bullets would have been.
    """
    if cfg.summarize.mode != "api":
        return ""
    provider = cfg.summarize.provider.strip()
    var = _SUMMARIZE_KEY_ENV.get(provider, "")
    if not var:
        return f"summarize.provider {provider!r} unknown"
    if not cfg.summarize.model.strip():
        return "summarize.model not set"
    if not cfg.summarize.api_key.strip() and not os.environ.get(var):
        return f"{var} not set"
    return ""


def resolved_summarize_api(cfg: Config) -> tuple[str, str]:
    """Effective ``(api_key, base_url)`` for api mode, so no caller re-derives the precedence.

    The environment wins over the literal key, as it does for embedding; ``base_url`` wins over
    ``OPENAI_BASE_URL`` (openai only, mirroring the embedding provider) and over the default.
    """
    provider = cfg.summarize.provider.strip()
    var = _SUMMARIZE_KEY_ENV.get(provider, "")
    api_key = (os.environ.get(var, "") if var else "") or cfg.summarize.api_key.strip()
    base_url = cfg.summarize.base_url.strip()
    if not base_url and provider == "openai":
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    return api_key, base_url or _SUMMARIZE_BASE_URL.get(provider, "")
