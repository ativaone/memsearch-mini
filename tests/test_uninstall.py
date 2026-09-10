"""Tests for ``uninstall.sh``.

Uninstalling has to be complete and surgical at the same time: everything the
plugin ever downloaded lives under ``$MEMSEARCH_HOME`` and goes away with
``--purge``, while hook entries and skills that belong to other tools — and
every project journal — survive untouched.

``HOME`` and ``MEMSEARCH_HOME`` both point inside ``tmp_path``, so no test can
reach the real ``~/.codex``, ``~/.agents`` or ``~/.memsearch``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

FAKE_UV = r"""#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "$FAKE_UV_LOG"
case "${1:-}" in
  run)
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        python|python3) shift; exec "$FAKE_PYTHON" "$@" ;;
        --project|--extra|--python) shift 2 ;;
        *) shift ;;
      esac
    done
    exit 0
    ;;
esac
exit 0
"""

# Everything uninstall.sh shells out to, so a PATH can be built without python3.
CORE_TOOLS = ("bash", "dirname", "rm", "grep", "du", "cut", "cat", "ls")


@dataclass
class Uninstaller:
    root: Path
    home: Path
    ms_home: Path
    env: dict[str, str]

    @property
    def hooks_file(self) -> Path:
        return self.home / ".codex" / "hooks.json"

    @property
    def config_file(self) -> Path:
        return self.home / ".codex" / "config.toml"

    @property
    def skill_dir(self) -> Path:
        return self.home / ".agents" / "skills" / "memory-recall"

    def run(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(self.root / "uninstall.sh"), *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(self.root),
            timeout=60,
        )

    def hooks(self) -> dict | list:
        return json.loads(self.hooks_file.read_text(encoding="utf-8"))

    def write_hooks(self, data: dict | list) -> None:
        self.hooks_file.parent.mkdir(parents=True, exist_ok=True)
        self.hooks_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def our_entry(self, script: str) -> dict:
        return {
            "matcher": "",
            "hooks": [{"type": "command", "command": f"bash {self.root}/hooks/{script} codex", "timeout": 10}],
        }

    def commands(self, event: str) -> list[str]:
        data = self.hooks()
        assert isinstance(data, dict)
        return [hook["command"] for entry in data["hooks"].get(event, []) for hook in entry["hooks"]]

    def install_skill(self, body: str) -> None:
        self.skill_dir.mkdir(parents=True, exist_ok=True)
        (self.skill_dir / "SKILL.md").write_text(body, encoding="utf-8")

    def fill_runtime_home(self) -> None:
        (self.ms_home / "venvs" / "abc123def456" / "bin").mkdir(parents=True, exist_ok=True)
        (self.ms_home / "venvs" / "abc123def456" / "bin" / "memsearch").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.ms_home / "uv-cache").mkdir(parents=True, exist_ok=True)
        (self.ms_home / "models" / "hub").mkdir(parents=True, exist_ok=True)
        (self.ms_home / "config.toml").write_text('[embedding]\nprovider = "onnx"\n', encoding="utf-8")


@pytest.fixture
def uninstaller(tmp_path: Path) -> Uninstaller:
    root = tmp_path / "checkout"
    root.mkdir()
    shutil.copytree(REPO / "bin", root / "bin")
    shutil.copytree(REPO / "hooks", root / "hooks")
    shutil.copytree(REPO / "codex", root / "codex")
    shutil.copy2(REPO / "uninstall.sh", root / "uninstall.sh")
    (root / "uninstall.sh").chmod(0o755)
    (root / "pyproject.toml").write_text('[project]\nname = "probe"\nversion = "0.0.0"\n', encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    home = tmp_path / "home"
    (home / ".memsearch").mkdir(parents=True, exist_ok=True)
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    ms_home = tmp_path / "ms-home"

    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "uv").write_text(FAKE_UV, encoding="utf-8")
    (bindir / "uv").chmod(0o755)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp_path / "tmp"),
        "LANG": "C.UTF-8",
        "MEMSEARCH_HOME": str(ms_home),
        "FAKE_UV_LOG": str(logs / "uv.log"),
        "FAKE_PYTHON": sys.executable,
    }
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return Uninstaller(root=root, home=home, ms_home=ms_home, env=env)


# --- Codex hook entries -------------------------------------------------------


def test_removes_only_our_hook_entries(uninstaller: Uninstaller) -> None:
    uninstaller.write_hooks(
        {
            "hooks": {
                "SessionStart": [
                    {"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]},
                    uninstaller.our_entry("session-start.sh"),
                ],
                "UserPromptSubmit": [uninstaller.our_entry("user-prompt-submit.sh")],
                "Stop": [uninstaller.our_entry("stop.sh")],
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo guard"}]}],
            }
        }
    )

    result = uninstaller.run()

    assert result.returncode == 0
    assert uninstaller.commands("SessionStart") == ["echo other"]
    assert uninstaller.commands("PreToolUse") == ["echo guard"]
    # Events left empty are dropped rather than left as dangling keys.
    assert "Stop" not in uninstaller.hooks()["hooks"]
    assert "UserPromptSubmit" not in uninstaller.hooks()["hooks"]
    assert "removed:" in result.stdout


def test_removes_entries_from_the_upstream_layout(uninstaller: Uninstaller) -> None:
    uninstaller.write_hooks(
        {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": "bash /old/clone/plugins/codex/hooks/stop.sh"}],
                    }
                ]
            }
        }
    )

    uninstaller.run()

    assert uninstaller.hooks() == {"hooks": {}}


def test_removes_entries_from_a_moved_checkout(uninstaller: Uninstaller) -> None:
    uninstaller.write_hooks(
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": "bash /gone/checkout/hooks/session-start.sh codex"}],
                    }
                ]
            }
        }
    )

    uninstaller.run()

    assert uninstaller.hooks() == {"hooks": {}}


def test_legacy_array_format_keeps_foreign_items(uninstaller: Uninstaller) -> None:
    uninstaller.write_hooks(
        [
            {"event": "Stop", "command": f"bash {uninstaller.root}/hooks/stop.sh codex", "timeout_ms": 30000},
            {"event": "Notification", "command": "echo notify"},
        ]
    )

    result = uninstaller.run()

    assert uninstaller.hooks() == [{"event": "Notification", "command": "echo notify"}]
    assert "removed:" in result.stdout


def test_invalid_hooks_json_is_left_alone(uninstaller: Uninstaller) -> None:
    uninstaller.hooks_file.parent.mkdir(parents=True, exist_ok=True)
    uninstaller.hooks_file.write_text("{not json", encoding="utf-8")

    result = uninstaller.run()

    assert result.returncode == 0
    assert uninstaller.hooks_file.read_text(encoding="utf-8") == "{not json"
    assert "not valid JSON" in result.stdout


def test_codex_feature_flag_is_never_touched(uninstaller: Uninstaller) -> None:
    uninstaller.config_file.parent.mkdir(parents=True, exist_ok=True)
    original = '[features]\nhooks = true\n\n[tui]\ntheme = "dark"\n'
    uninstaller.config_file.write_text(original, encoding="utf-8")

    result = uninstaller.run()

    assert uninstaller.config_file.read_text(encoding="utf-8") == original
    assert "[features] hooks" in result.stdout
    assert "left as it is" in result.stdout


# --- Codex skill --------------------------------------------------------------


def test_removes_our_skill(uninstaller: Uninstaller) -> None:
    uninstaller.install_skill(f"---\nname: memory-recall\n---\nRun {uninstaller.root}/bin/memsearch search\n")

    result = uninstaller.run()

    assert not uninstaller.skill_dir.exists()
    assert "removed" in result.stdout


def test_keeps_a_foreign_skill_of_the_same_name(uninstaller: Uninstaller) -> None:
    uninstaller.install_skill("---\nname: memory-recall\n---\nSomebody else's recall skill\n")

    result = uninstaller.run()

    assert uninstaller.skill_dir.exists()
    assert "not a memsearch skill" in result.stdout


# --- runtime home -------------------------------------------------------------


def test_purge_removes_the_whole_runtime_home(uninstaller: Uninstaller) -> None:
    uninstaller.fill_runtime_home()

    result = uninstaller.run("--purge")

    assert result.returncode == 0
    assert not uninstaller.ms_home.exists()
    assert "removed" in result.stdout


def test_without_purge_the_runtime_home_stays_and_is_explained(uninstaller: Uninstaller) -> None:
    uninstaller.fill_runtime_home()

    result = uninstaller.run()

    assert (uninstaller.ms_home / "config.toml").exists()
    assert f"rm -rf {uninstaller.ms_home}" in result.stdout
    assert "config.toml" in result.stdout


def test_purge_never_touches_project_journals(uninstaller: Uninstaller, tmp_path: Path) -> None:
    project = tmp_path / "some-project" / ".memsearch"
    (project / "memory").mkdir(parents=True)
    journal = project / "memory" / "2026-09-09.md"
    journal.write_text("## Session 10:00\n\n- kept\n", encoding="utf-8")
    (project / "index.db").write_text("derived", encoding="utf-8")
    uninstaller.fill_runtime_home()

    result = uninstaller.run("--purge")

    assert journal.read_text(encoding="utf-8") == "## Session 10:00\n\n- kept\n"
    assert (project / "index.db").exists()
    assert "Project journals are never touched" in result.stdout
    assert "index.db-wal" in result.stdout  # how to drop a derived index by hand


# --- shape --------------------------------------------------------------------


def test_is_idempotent_and_safe_when_nothing_is_installed(uninstaller: Uninstaller) -> None:
    first = uninstaller.run()
    second = uninstaller.run("--purge")

    assert first.returncode == 0
    assert second.returncode == 0
    assert "nothing to unwire" in first.stdout
    assert not uninstaller.hooks_file.exists()
    assert not uninstaller.skill_dir.exists()


def test_prints_the_claude_code_commands(uninstaller: Uninstaller) -> None:
    result = uninstaller.run()

    assert "/plugin uninstall memsearch-mini@ativaone" in result.stdout
    assert "/plugin marketplace remove ativaone" in result.stdout


def test_unknown_argument_is_rejected(uninstaller: Uninstaller) -> None:
    result = uninstaller.run("--nuke")

    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_hook_removal_falls_back_to_uv_run_python(uninstaller: Uninstaller, tmp_path: Path) -> None:
    """No system python3: the JSON edit goes through the interpreter uv manages."""
    slim = tmp_path / "slimbin"
    slim.mkdir()
    for tool in CORE_TOOLS:
        found = shutil.which(tool)
        if found:
            (slim / tool).symlink_to(found)
    shutil.copy2(Path(uninstaller.env["PATH"].split(":")[0]) / "uv", slim / "uv")
    (slim / "uv").chmod(0o755)
    assert shutil.which("python3", path=str(slim)) is None
    uninstaller.write_hooks({"hooks": {"Stop": [uninstaller.our_entry("stop.sh")]}})

    result = uninstaller.run(extra_env={"PATH": str(slim)})

    assert result.returncode == 0
    assert uninstaller.hooks() == {"hooks": {}}
    uv_log = Path(uninstaller.env["FAKE_UV_LOG"]).read_text(encoding="utf-8")
    assert "run " in uv_log
