"""Tests for ``codex/install.sh``.

The installer only ever writes to ``$HOME``, so every test runs with ``HOME``
pointed inside ``tmp_path`` and a fake ``uv`` first on PATH. Nothing here can
touch the real ``~/.codex``, ``~/.agents`` or the real uv.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Minimal fake uv: `command -v uv` must succeed, `uv sync` must materialise a
# CLI, and `uv run ... python -` must reach a real interpreter (the installer
# falls back to it when the host has no python3).
FAKE_UV = r"""#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "$FAKE_UV_LOG"
case "${1:-}" in
  sync)
    mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
    printf '#!/bin/sh\nexit 0\n' > "$UV_PROJECT_ENVIRONMENT/bin/memsearch"
    chmod +x "$UV_PROJECT_ENVIRONMENT/bin/memsearch"
    exit 0
    ;;
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

# Everything codex/install.sh shells out to, so a PATH can be built without a
# system python3 while the installer still works.
CORE_TOOLS = ("bash", "dirname", "cp", "mkdir", "rm", "chmod", "cat", "sed", "grep", "ls")


@dataclass
class Installer:
    root: Path
    home: Path
    env: dict[str, str]

    @property
    def hooks_file(self) -> Path:
        return self.home / ".codex" / "hooks.json"

    @property
    def config_file(self) -> Path:
        return self.home / ".codex" / "config.toml"

    @property
    def skill(self) -> Path:
        return self.home / ".agents" / "skills" / "memory-recall" / "SKILL.md"

    def run(self, extra_env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess:
        env = dict(self.env)
        env.update(extra_env or {})
        result = subprocess.run(
            ["bash", str(self.root / "codex" / "install.sh")],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(self.root),
            timeout=60,
        )
        if check:
            assert result.returncode == 0, f"installer failed:\n{result.stdout}\n{result.stderr}"
        return result

    def hooks(self) -> dict:
        return json.loads(self.hooks_file.read_text(encoding="utf-8"))

    def commands(self, event: str) -> list[str]:
        entries = self.hooks()["hooks"].get(event, [])
        return [hook["command"] for entry in entries for hook in entry["hooks"]]

    def memsearch_commands(self, event: str) -> list[str]:
        return [command for command in self.commands(event) if str(self.root) in command]

    def uv_calls(self) -> list[str]:
        log = Path(self.env["FAKE_UV_LOG"])
        if not log.exists():
            return []
        return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def installer(tmp_path: Path) -> Installer:
    root = tmp_path / "checkout"
    root.mkdir()
    shutil.copytree(REPO / "bin", root / "bin")
    shutil.copytree(REPO / "hooks", root / "hooks")
    shutil.copytree(REPO / "codex", root / "codex")
    (root / "pyproject.toml").write_text('[project]\nname = "probe"\nversion = "0.0.0"\n', encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    # Deliberately unset: the installer is what makes them executable again
    # (tarball extraction and `cp -r` can drop the bit).
    for script in [root / "bin" / "memsearch", *(root / "hooks").glob("*.sh")]:
        script.chmod(0o644)

    home = tmp_path / "home"
    (home / ".memsearch").mkdir(parents=True, exist_ok=True)
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)

    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "uv").write_text(FAKE_UV, encoding="utf-8")
    (bindir / "uv").chmod(0o755)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp_path / "tmp"),
        "LANG": "C.UTF-8",
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv"),
        "MEMSEARCH_CONFIG": str(home / ".memsearch" / "config.toml"),
        "MEMSEARCH_SKIP_SYNC": "1",
        "FAKE_UV_LOG": str(logs / "uv.log"),
        "FAKE_PYTHON": sys.executable,
    }
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return Installer(root=root, home=home, env=env)


EXPECTED = {
    "SessionStart": ("session-start.sh", 10),
    "UserPromptSubmit": ("user-prompt-submit.sh", 5),
    "Stop": ("stop.sh", 30),
}


def test_fresh_install_writes_hooks_skill_and_flag(installer: Installer) -> None:
    installer.run()

    for event, (script, timeout) in EXPECTED.items():
        entries = installer.hooks()["hooks"][event]
        assert len(entries) == 1
        hook = entries[0]["hooks"][0]
        assert hook["command"] == f"bash {installer.root}/hooks/{script} codex"
        assert hook["timeout"] == timeout
        assert hook["type"] == "command"
        assert "async" not in hook

    assert "hooks = true" in installer.config_file.read_text(encoding="utf-8")
    skill = installer.skill.read_text(encoding="utf-8")
    assert "__INSTALL_DIR__" not in skill
    assert f"{installer.root}/bin/memsearch" in skill


def test_install_is_idempotent(installer: Installer) -> None:
    installer.run()
    first = installer.hooks()
    installer.run()

    assert installer.hooks() == first
    for event in EXPECTED:
        assert len(installer.memsearch_commands(event)) == 1
    # The second run backs the first result up before rewriting it.
    assert (installer.hooks_file.with_suffix(".json.bak")).exists()
    assert not installer.hooks_file.with_name("hooks.json.tmp").exists()


def test_foreign_hooks_are_preserved(installer: Installer) -> None:
    installer.hooks_file.parent.mkdir(parents=True, exist_ok=True)
    installer.hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "echo other"}]}],
                    "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo guard"}]}],
                }
            }
        ),
        encoding="utf-8",
    )

    installer.run()

    assert "echo other" in installer.commands("SessionStart")
    assert installer.commands("PreToolUse") == ["echo guard"]
    assert len(installer.memsearch_commands("SessionStart")) == 1


def test_legacy_upstream_entries_are_removed(installer: Installer) -> None:
    installer.hooks_file.parent.mkdir(parents=True, exist_ok=True)
    installer.hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "",
                            "hooks": [
                                {"type": "command", "command": "bash /old/clone/plugins/codex/hooks/stop.sh"},
                            ],
                        }
                    ],
                    "SessionStart": [
                        {
                            "matcher": "",
                            "hooks": [
                                {"type": "command", "command": "bash /moved/checkout/hooks/session-start.sh codex"},
                            ],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    installer.run()

    assert installer.commands("Stop") == [f"bash {installer.root}/hooks/stop.sh codex"]
    assert installer.commands("SessionStart") == [f"bash {installer.root}/hooks/session-start.sh codex"]


def test_legacy_array_format_is_converted(installer: Installer) -> None:
    installer.hooks_file.parent.mkdir(parents=True, exist_ok=True)
    installer.hooks_file.write_text(
        json.dumps(
            [
                {"event": "Stop", "command": "echo legacy", "timeout_ms": 1500},
                {"event": "Notification", "command": "echo notify", "async": True},
            ]
        ),
        encoding="utf-8",
    )

    installer.run()

    data = installer.hooks()
    assert isinstance(data, dict)
    legacy = data["hooks"]["Notification"][0]["hooks"][0]
    assert legacy["command"] == "echo notify"
    assert legacy["async"] is True
    stop = data["hooks"]["Stop"][0]["hooks"][0]
    assert stop["command"] == "echo legacy"
    assert stop["timeout"] == 2  # 1500 ms rounds up to whole seconds
    assert len(installer.memsearch_commands("Stop")) == 1


def test_unreadable_hooks_file_is_replaced_not_crashed(installer: Installer) -> None:
    installer.hooks_file.parent.mkdir(parents=True, exist_ok=True)
    installer.hooks_file.write_text("not json at all", encoding="utf-8")

    installer.run()

    assert len(installer.memsearch_commands("Stop")) == 1
    assert installer.hooks_file.with_suffix(".json.bak").read_text(encoding="utf-8") == "not json at all"


def test_existing_config_keeps_other_sections_and_drops_legacy_flag(installer: Installer) -> None:
    installer.config_file.parent.mkdir(parents=True, exist_ok=True)
    installer.config_file.write_text(
        'model = "gpt-5.1-codex"\n\n[features]\ncodex_hooks = true\nhooks = false\nweb_search = true\n\n[tui]\ntheme = "dark"\n',
        encoding="utf-8",
    )

    installer.run()

    text = installer.config_file.read_text(encoding="utf-8")
    assert "hooks = true" in text
    assert "hooks = false" not in text
    assert "codex_hooks" not in text
    assert 'model = "gpt-5.1-codex"' in text
    assert "web_search = true" in text
    assert 'theme = "dark"' in text


def test_config_without_features_section_gains_one(installer: Installer) -> None:
    installer.config_file.parent.mkdir(parents=True, exist_ok=True)
    installer.config_file.write_text('model = "gpt-5.1-codex"\n', encoding="utf-8")

    installer.run()

    text = installer.config_file.read_text(encoding="utf-8")
    assert "[features]" in text
    assert "hooks = true" in text
    assert 'model = "gpt-5.1-codex"' in text


def test_existing_skill_directory_is_replaced(installer: Installer) -> None:
    installer.skill.parent.mkdir(parents=True, exist_ok=True)
    installer.skill.write_text("stale skill\n", encoding="utf-8")
    (installer.skill.parent / "leftover.md").write_text("stale\n", encoding="utf-8")

    installer.run()

    assert "stale skill" not in installer.skill.read_text(encoding="utf-8")
    assert not (installer.skill.parent / "leftover.md").exists()


def test_scripts_are_executable_after_install(installer: Installer) -> None:
    installer.run()

    for script in [installer.root / "bin" / "memsearch", *(installer.root / "hooks").glob("*.sh")]:
        assert os.access(script, os.X_OK), f"{script} is not executable"


def test_skip_sync_avoids_the_blocking_sync(installer: Installer) -> None:
    installer.run()

    assert [call for call in installer.uv_calls() if call.startswith("sync")] == []
    assert not (Path(installer.env["UV_PROJECT_ENVIRONMENT"]) / "bin" / "memsearch").exists()


def test_without_skip_sync_the_runtime_is_synced_first(installer: Installer) -> None:
    installer.run(extra_env={"MEMSEARCH_SKIP_SYNC": "0"})

    assert [call for call in installer.uv_calls() if call.startswith("sync")] == [
        f"sync --project {installer.root} --frozen --extra onnx"
    ]
    assert (Path(installer.env["UV_PROJECT_ENVIRONMENT"]) / "bin" / "memsearch").exists()


def test_missing_uv_aborts_before_touching_home(installer: Installer) -> None:
    result = installer.run(extra_env={"PATH": "/usr/bin:/bin"}, check=False)

    assert result.returncode == 1
    assert "uv not found" in result.stdout
    assert "docs.astral.sh/uv" in result.stdout
    assert not installer.hooks_file.exists()
    assert not installer.skill.exists()


def test_json_merge_falls_back_to_uv_run_python(installer: Installer, tmp_path: Path) -> None:
    """No system python3: the edits go through the interpreter uv manages."""
    slim = tmp_path / "slimbin"
    slim.mkdir()
    for tool in CORE_TOOLS:
        found = shutil.which(tool)
        if found:
            (slim / tool).symlink_to(found)
    shutil.copy2(Path(installer.env["PATH"].split(":")[0]) / "uv", slim / "uv")
    (slim / "uv").chmod(0o755)
    assert shutil.which("python3", path=str(slim)) is None

    installer.run(extra_env={"PATH": str(slim)})

    assert len(installer.memsearch_commands("Stop")) == 1
    assert "hooks = true" in installer.config_file.read_text(encoding="utf-8")
    assert "__INSTALL_DIR__" not in installer.skill.read_text(encoding="utf-8")
    assert any(call.startswith("run ") for call in installer.uv_calls())
