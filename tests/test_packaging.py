"""Packaging invariants for the repo-as-plugin layout.

The repository root is the Claude Code plugin root: ``.claude-plugin/`` holds
the manifests, ``hooks/hooks.json`` and ``skills/`` are auto-discovered from
there, and ``codex/install.sh`` wires the same scripts into Codex. These tests
are the seam between the shell, the manifests and pyproject.toml.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

REPO = Path(__file__).resolve().parents[1]

PLUGIN_JSON = REPO / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = REPO / ".claude-plugin" / "marketplace.json"
HOOKS_JSON = REPO / "hooks" / "hooks.json"
CLAUDE_SKILL = REPO / "skills" / "memory-recall" / "SKILL.md"
CODEX_SKILL = REPO / "codex" / "skills" / "memory-recall" / "SKILL.md"
LAUNCHERS = ("session-start.sh", "user-prompt-submit.sh", "stop.sh")
RECALL_HINT = "[memsearch-mini] Recall available if needed"

EXPECTED_HOOKS = {
    "SessionStart": ("session-start.sh", 10, False),
    "UserPromptSubmit": ("user-prompt-submit.sh", 5, False),
    "Stop": ("stop.sh", 120, True),
}


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _pyproject() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no YAML frontmatter"
    block = text.split("---\n", 2)[1]
    fields = {}
    for line in block.splitlines():
        if not line.strip() or line.startswith(("#", " ")):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"')
    return fields


# --- versions -----------------------------------------------------------------


def test_version_is_the_same_everywhere() -> None:
    version = _pyproject()["project"]["version"]
    plugin = _json(PLUGIN_JSON)
    marketplace = _json(MARKETPLACE_JSON)

    assert plugin["version"] == version
    assert marketplace["metadata"]["version"] == version
    assert [entry["version"] for entry in marketplace["plugins"]] == [version]


def test_plugin_name_matches_the_marketplace_entry() -> None:
    assert _json(PLUGIN_JSON)["name"] == "memsearch-mini"
    assert [entry["name"] for entry in _json(MARKETPLACE_JSON)["plugins"]] == ["memsearch-mini"]


def test_project_ships_the_memsearch_mini_cli() -> None:
    assert _pyproject()["project"]["scripts"]["memsearch-mini"] == "memsearch_mini.cli:cli"


# --- manifests ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [PLUGIN_JSON, MARKETPLACE_JSON, HOOKS_JSON],
    ids=lambda p: p.name,
)
def test_manifest_is_valid_json(path: Path) -> None:
    assert path.stat().st_size > 0
    assert isinstance(_json(path), dict)


def test_marketplace_source_points_at_this_repository() -> None:
    entry = _json(MARKETPLACE_JSON)["plugins"][0]

    assert entry["source"] == "./"
    resolved = (MARKETPLACE_JSON.parent.parent / entry["source"]).resolve()
    assert (resolved / ".claude-plugin" / "plugin.json").is_file()
    assert (resolved / "hooks" / "hooks.json").is_file()
    assert (resolved / "skills" / "memory-recall" / "SKILL.md").is_file()


def test_hooks_json_matches_the_launchers() -> None:
    hooks = _json(HOOKS_JSON)["hooks"]

    assert set(hooks) == set(EXPECTED_HOOKS)
    for event, (script, timeout, is_async) in EXPECTED_HOOKS.items():
        entries = hooks[event]
        assert len(entries) == 1
        hook = entries[0]["hooks"][0]
        assert hook["type"] == "command"
        assert hook["command"] == f'bash "${{CLAUDE_PLUGIN_ROOT}}/hooks/{script}" claude'
        assert hook["timeout"] == timeout
        assert hook.get("async", False) is is_async
        assert (REPO / "hooks" / script).is_file()


def test_no_session_end_hook() -> None:
    assert "SessionEnd" not in _json(HOOKS_JSON)["hooks"]
    assert not (REPO / "hooks" / "session-end.sh").exists()


# --- shell layout -------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    ["bin/memsearch-mini", "codex/install.sh", "uninstall.sh", *(f"hooks/{name}" for name in LAUNCHERS)],
)
def test_scripts_are_executable(relative: str) -> None:
    path = REPO / relative
    assert path.is_file()
    assert os.access(path, os.X_OK), f"{relative} is missing its executable bit"


def test_shell_entry_points_are_lf_only() -> None:
    """A CRLF clone makes every launcher unrunnable: `bash\\r: no such file`."""
    attributes = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes
    # bin/memsearch-mini has no .sh suffix, so the glob above never covered it.
    assert "bin/memsearch-mini text eol=lf" in attributes

    for path in [REPO / "bin" / "memsearch-mini", *sorted((REPO / "hooks").glob("*.sh"))]:
        assert b"\r\n" not in path.read_bytes(), f"{path.name} has CRLF line endings"


def test_common_sh_is_a_library_no_host_ever_executes() -> None:
    """common.sh is sourced, never run: no hook command may point at it."""
    commands = [
        hook["command"]
        for entries in _json(HOOKS_JSON)["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]

    assert (REPO / "hooks" / "common.sh").is_file()
    assert all("common.sh" not in command for command in commands)


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launchers_never_use_set_e(name: str) -> None:
    text = (REPO / "hooks" / name).read_text(encoding="utf-8")

    assert "set -uo pipefail" in text
    assert not re.search(r"^\s*set -[a-z]*e", text, re.MULTILINE)
    assert 'source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"' in text
    assert "hook_guard" in text


def test_common_sh_has_no_side_effects_on_source() -> None:
    text = (REPO / "hooks" / "common.sh").read_text(encoding="utf-8")

    assert not re.search(r"^\s*set -[a-z]*e", text, re.MULTILINE)
    # No stdin reading anywhere: Python owns the payload. A `cat` with no file
    # argument is what upstream used to drain stdin inside the shell.
    assert re.search(r"\bcat\b(?![ \t]+[\"'$])", text) is None
    assert "INPUT=" not in text
    for function in ("have_uv", "hook_guard", "extras_args", "venv_ready", "sync_now", "sync_detached"):
        assert re.search(rf"^{function}\(\) \{{", text, re.MULTILINE), f"{function} is missing"


def test_detached_child_redirects_all_three_fds() -> None:
    text = (REPO / "hooks" / "common.sh").read_text(encoding="utf-8")

    assert '</dev/null >>"$SYNC_LOG" 2>&1 &' in text


def test_capability_hint_string_is_shared_by_hook_and_skill() -> None:
    assert RECALL_HINT in (REPO / "hooks" / "user-prompt-submit.sh").read_text(encoding="utf-8")
    assert RECALL_HINT in CLAUDE_SKILL.read_text(encoding="utf-8")
    assert RECALL_HINT in CODEX_SKILL.read_text(encoding="utf-8")


# --- skills -------------------------------------------------------------------


def test_claude_skill_frontmatter() -> None:
    fields = _frontmatter(CLAUDE_SKILL)

    assert fields["name"] == "memory-recall"
    assert fields["context"] == "fork"
    assert fields["allowed-tools"] == "Bash"
    assert RECALL_HINT in fields["description"]


def test_codex_skill_frontmatter_has_name_and_description_only() -> None:
    assert set(_frontmatter(CODEX_SKILL)) == {"name", "description"}


def test_skills_share_the_same_description() -> None:
    assert _frontmatter(CLAUDE_SKILL)["description"] == _frontmatter(CODEX_SKILL)["description"]


def test_claude_skill_calls_the_checkout_cli() -> None:
    text = CLAUDE_SKILL.read_text(encoding="utf-8")

    assert '${CLAUDE_PLUGIN_ROOT}/bin/memsearch-mini search "<query>" -k 5 --json' in text
    assert "${CLAUDE_PLUGIN_ROOT}/bin/memsearch-mini expand <chunk_id> --json" in text
    assert "${CLAUDE_PLUGIN_ROOT}/bin/memsearch-mini transcript <path> --turn <uuid> --context 3" in text
    assert "No relevant memories found." in text


def test_codex_skill_calls_the_installed_checkout() -> None:
    text = CODEX_SKILL.read_text(encoding="utf-8")

    assert '__INSTALL_DIR__/bin/memsearch-mini search "<query>" -k 5 --json' in text
    assert "__INSTALL_DIR__/bin/memsearch-mini expand <chunk_id> --json" in text
    assert "__INSTALL_DIR__/bin/memsearch-mini transcript <rollout_path>" in text
    assert "--turn" not in text  # Codex rollouts have no per-turn uuid anchor
    assert "No relevant memories found." in text


@pytest.mark.parametrize("path", [CLAUDE_SKILL, CODEX_SKILL], ids=["claude", "codex"])
def test_skills_document_the_raw_markdown_fallback(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert 'MDIR="${MEMSEARCH_MINI_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.memsearch-mini}"' in text
    assert "source of truth" in text


# --- fork hygiene -------------------------------------------------------------

SHELL_AND_SKILL_FILES = [
    REPO / "hooks" / "common.sh",
    *(REPO / "hooks" / name for name in LAUNCHERS),
    REPO / "bin" / "memsearch-mini",
    REPO / "codex" / "install.sh",
    CLAUDE_SKILL,
    CODEX_SKILL,
]


@pytest.mark.parametrize(
    "banned",
    ["uvx", "--default-collection", "milvus", "--json-output", "--top-k", "derive-collection"],
)
def test_upstream_concepts_are_gone(banned: str) -> None:
    offenders = [path.name for path in SHELL_AND_SKILL_FILES if banned in path.read_text(encoding="utf-8")]

    assert offenders == []


def test_summarize_prompt_template_ships_with_the_plugin() -> None:
    template = (REPO / "prompts" / "summarize.txt").read_text(encoding="utf-8")

    assert "{{AGENT_NAME}}" in template


def test_installer_points_at_the_uninstaller() -> None:
    installer = (REPO / "codex" / "install.sh").read_text(encoding="utf-8")

    assert "uninstall.sh" in installer
    assert "$MEMSEARCH_MINI_HOME" in installer


def test_everything_downloadable_is_redirected_into_one_home() -> None:
    """One directory to delete: no cache may land in the user's own caches."""
    library = (REPO / "hooks" / "common.sh").read_text(encoding="utf-8")

    assert 'MEMSEARCH_MINI_HOME="${MEMSEARCH_MINI_HOME:-$HOME/.memsearch-mini}"' in library
    for line in (
        ': "${UV_CACHE_DIR:=$MEMSEARCH_MINI_HOME/uv-cache}"',
        ': "${UV_PYTHON_INSTALL_DIR:=$MEMSEARCH_MINI_HOME/python}"',
        ': "${HF_HOME:=$MEMSEARCH_MINI_HOME/models}"',
    ):
        assert line in library
    assert "export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR HF_HOME" in library
    assert 'export UV_PROJECT_ENVIRONMENT="$VENV"' in library
    # The checkout is never written to: no default environment inside $ROOT.
    assert "$ROOT/.venv" not in library


def test_readiness_is_tied_to_the_configured_extras() -> None:
    """A provider switch must resync on its own: the stamp records the extras."""
    library = (REPO / "hooks" / "common.sh").read_text(encoding="utf-8")

    assert '[ "$(cat "$SYNC_STAMP" 2>/dev/null)" = "$(extras_args)" ]' in library
    assert 'extras_args >"$SYNC_STAMP"' in library
    assert 'touch "$SYNC_STAMP"' not in library
