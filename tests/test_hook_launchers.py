"""Tests for the plugin shell: ``hooks/*.sh`` and ``bin/memsearch-mini``.

The launchers run as real processes against a copy of the real scripts, with a
fake ``uv`` first on PATH, a temporary ``HOME``, a temporary
``UV_PROJECT_ENVIRONMENT`` and a temporary ``MEMSEARCH_MINI_CONFIG``. No test can
reach the real uv, the real home, the real config or the real project
environment.

Contract under test — the shell is the only place that enforces it:

* a launcher never runs under ``set -e``; it always reaches its ``printf``;
* on its own code paths it prints exactly one JSON object and exits 0;
* once the runtime is ready the launcher ``exec``s Python, so from that point
  the exit code and stdout belong to Python. A launcher cannot rewrite them;
  the ``hook`` command group is what guarantees ``{}`` there. What the shell
  still guarantees in that case is that stdout is either empty (a no-op for
  every host) or whatever Python wrote — never a half-written line of its own;
* a detached child redirects all three fds, so the hook runner never waits on
  a pipe the child still holds.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

UV_MISSING = "[memsearch-mini] uv not found on PATH"
INSTALLING = "[memsearch-mini] installing runtime in the background"
RECALL_HINT = "[memsearch-mini] Recall available if needed"

# Fake uv. Logs every invocation, then emulates the two subcommands the plugin
# shell uses: `uv sync` materialises the fake CLI inside the environment,
# `uv run` execs it (or a real python, which codex/install.sh may ask for).
FAKE_UV = r"""#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "$FAKE_UV_LOG"
{
  printf 'UV_PROJECT_ENVIRONMENT=%s\n' "${UV_PROJECT_ENVIRONMENT-}"
  printf 'UV_CACHE_DIR=%s\n' "${UV_CACHE_DIR-}"
  printf 'UV_PYTHON_INSTALL_DIR=%s\n' "${UV_PYTHON_INSTALL_DIR-}"
  printf 'HF_HOME=%s\n' "${HF_HOME-}"
  printf 'MEMSEARCH_MINI_HOME=%s\n' "${MEMSEARCH_MINI_HOME-}"
} >> "$FAKE_ENV_LOG"
case "${1:-}" in
  sync)
    if [ -n "${FAKE_SYNC_SLEEP:-}" ]; then sleep "$FAKE_SYNC_SLEEP"; fi
    if [ "${FAKE_SYNC_RC:-0}" = "0" ]; then
      mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
      cp "$FAKE_MEMSEARCH_MINI_MINI" "$UV_PROJECT_ENVIRONMENT/bin/memsearch-mini"
      chmod +x "$UV_PROJECT_ENVIRONMENT/bin/memsearch-mini"
    fi
    exit "${FAKE_SYNC_RC:-0}"
    ;;
  run)
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        memsearch-mini) shift; exec "$UV_PROJECT_ENVIRONMENT/bin/memsearch-mini" "$@" ;;
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

# Fake memsearch-mini CLI. Logs argv, drains stdin the way hooks.read_payload does
# (bounded by a select timeout, so an open pipe cannot hang the test), then
# prints $FAKE_STDOUT (default "{}") and exits $FAKE_RC.
FAKE_MEMSEARCH_MINI_MINI = """#!__PYTHON__
import os
import select
import sys
import time

with open(os.environ["FAKE_MEMSEARCH_MINI_LOG"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")

buf = b""
deadline = time.time() + 1.0
try:
    fd = sys.stdin.fileno()
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        if not select.select([fd], [], [], remaining)[0]:
            break
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        buf += chunk
except Exception:
    pass
with open(os.environ["FAKE_MEMSEARCH_MINI_STDIN_LOG"], "ab") as fh:
    fh.write(buf)

out = os.environ.get("FAKE_STDOUT", "{}")
if out:
    sys.stdout.write(out)
    sys.stdout.flush()
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""


@dataclass
class Plugin:
    """A throwaway copy of the plugin shell wired to fake binaries."""

    root: Path
    home: Path
    venv: Path
    env: dict[str, str]

    @property
    def uv_log(self) -> Path:
        return Path(self.env["FAKE_UV_LOG"])

    @property
    def cli_log(self) -> Path:
        return Path(self.env["FAKE_MEMSEARCH_MINI_LOG"])

    @property
    def stdin_log(self) -> Path:
        return Path(self.env["FAKE_MEMSEARCH_MINI_STDIN_LOG"])

    @property
    def sync_log(self) -> Path:
        return self.venv.with_name(self.venv.name + ".log")

    @property
    def root_file(self) -> Path:
        """Provenance sidecar: which checkout the environment belongs to."""
        return self.venv.with_name(self.venv.name + ".root")

    @property
    def ms_home(self) -> Path:
        return Path(self.env["MEMSEARCH_MINI_HOME"])

    @property
    def default_venv(self) -> Path:
        """Where common.sh puts the environment when the user sets no override."""
        digest = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        return self.ms_home / "venvs" / digest

    def uv_env(self) -> dict[str, str]:
        """The environment the last uv invocation actually saw."""
        seen: dict[str, str] = {}
        for line in self._read(Path(self.env["FAKE_ENV_LOG"])):
            key, _, value = line.partition("=")
            seen[key] = value
        return seen

    def _read(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def uv_calls(self) -> list[str]:
        return self._read(self.uv_log)

    def sync_calls(self) -> list[str]:
        return [line for line in self.uv_calls() if line.startswith("sync")]

    def cli_calls(self) -> list[str]:
        return self._read(self.cli_log)

    @property
    def stamp(self) -> Path:
        return self.venv / ".memsearch-mini-synced"

    def mark_ready(self, extras: str = "--extra onnx") -> None:
        """Materialise a synced environment the way `sync_now` would.

        The stamp holds the extras the environment was built with; readiness
        means "built with exactly the extras the current config asks for".
        """
        bindir = self.venv / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.env["FAKE_MEMSEARCH_MINI_MINI"], bindir / "memsearch-mini")
        (bindir / "memsearch-mini").chmod(0o755)
        self.stamp.write_text(extras + "\n", encoding="utf-8")
        newer = time.time() + 5
        os.utime(self.stamp, (newer, newer))

    def _env(self, extra_env: dict[str, str] | None, drop_env: tuple[str, ...]) -> dict[str, str]:
        env = dict(self.env)
        env.update(extra_env or {})
        for name in drop_env:
            env.pop(name, None)
        return env

    def run(
        self,
        script: str,
        *args: str,
        stdin: str | None = "",
        extra_env: dict[str, str] | None = None,
        drop_env: tuple[str, ...] = (),
        timeout: int = 20,
    ) -> subprocess.CompletedProcess:
        env = self._env(extra_env, drop_env)
        target = self.root / "hooks" / script if script.endswith(".sh") else self.root / script
        return subprocess.run(
            ["bash", str(target), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(self.root),
            timeout=timeout,
        )

    def bash(
        self,
        snippet: str,
        extra_env: dict[str, str] | None = None,
        drop_env: tuple[str, ...] = (),
        timeout: int = 20,
    ):
        env = self._env(extra_env, drop_env)
        return subprocess.run(
            ["bash", "-c", f'source "{self.root}/hooks/common.sh"\n{snippet}'],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(self.root),
            timeout=timeout,
        )

    def wait_for(self, predicate, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()


@pytest.fixture
def plugin(tmp_path: Path) -> Plugin:
    root = tmp_path / "plugin"
    root.mkdir()
    shutil.copytree(REPO / "bin", root / "bin")
    shutil.copytree(REPO / "hooks", root / "hooks")
    for script in [root / "bin" / "memsearch-mini", *(root / "hooks").glob("*.sh")]:
        script.chmod(0o755)
    (root / "pyproject.toml").write_text('[project]\nname = "probe"\nversion = "0.0.0"\n', encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    # conftest's isolated_home fixture may have created these already.
    home = tmp_path / "home"
    (home / ".memsearch-mini").mkdir(parents=True, exist_ok=True)
    tmp = tmp_path / "tmp"
    tmp.mkdir(exist_ok=True)
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)

    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    (bindir / "uv").write_text(FAKE_UV, encoding="utf-8")
    (bindir / "uv").chmod(0o755)
    fake_cli = tmp_path / "fake-memsearch-mini"
    fake_cli.write_text(FAKE_MEMSEARCH_MINI_MINI.replace("__PYTHON__", sys.executable), encoding="utf-8")
    fake_cli.chmod(0o755)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "MEMSEARCH_MINI_HOME": str(tmp_path / "ms-home"),
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv"),
        "MEMSEARCH_MINI_CONFIG": str(home / ".memsearch-mini" / "config.toml"),
        "FAKE_UV_LOG": str(logs / "uv.log"),
        "FAKE_ENV_LOG": str(logs / "env.log"),
        "FAKE_MEMSEARCH_MINI_LOG": str(logs / "cli.log"),
        "FAKE_MEMSEARCH_MINI_STDIN_LOG": str(logs / "stdin.log"),
        "FAKE_MEMSEARCH_MINI_MINI": str(fake_cli),
        "FAKE_PYTHON": sys.executable,
    }
    return Plugin(root=root, home=home, venv=tmp_path / "venv", env=env)


# --- ready runtime: the launcher execs the CLI --------------------------------


def test_session_start_execs_cli_with_platform_and_stdin(plugin: Plugin) -> None:
    plugin.mark_ready()
    payload = '{"session_id": "s1", "cwd": "/tmp/proj", "unicode": "北京"}'

    result = plugin.run("session-start.sh", "claude", stdin=payload)

    assert result.returncode == 0
    assert result.stdout == "{}"
    assert plugin.cli_calls() == ["hook session-start --platform claude"]
    assert plugin.stdin_log.read_text(encoding="utf-8") == payload


def test_stop_passes_platform_from_argv(plugin: Plugin) -> None:
    plugin.mark_ready()

    result = plugin.run("stop.sh", "codex", stdin='{"stop_hook_active": false}')

    assert result.returncode == 0
    assert plugin.cli_calls() == ["hook stop --platform codex"]
    assert plugin.stdin_log.read_text(encoding="utf-8") == '{"stop_hook_active": false}'


def test_platform_defaults_to_claude(plugin: Plugin) -> None:
    plugin.mark_ready()

    plugin.run("stop.sh", stdin="{}")

    assert plugin.cli_calls() == ["hook stop --platform claude"]


def test_uv_run_is_frozen_and_carries_extras(plugin: Plugin) -> None:
    plugin.mark_ready()

    plugin.run("session-start.sh", "claude", stdin="{}")

    call = plugin.uv_calls()[-1]
    assert call.startswith("run --project ")
    assert str(plugin.root) in call
    assert "--frozen --no-sync" in call
    assert "--extra onnx" in call
    # `--no-sync` is what keeps the hot path at tens of milliseconds.
    assert "sync" not in [c.split()[0] for c in plugin.uv_calls()]


def test_user_prompt_submit_prints_hint_without_touching_python(plugin: Plugin) -> None:
    plugin.mark_ready()

    result = plugin.run("user-prompt-submit.sh", "claude", stdin='{"prompt": "hi"}')

    assert result.returncode == 0
    assert json.loads(result.stdout)["systemMessage"] == RECALL_HINT
    assert plugin.uv_calls() == []
    assert plugin.cli_calls() == []


# --- host-facing robustness ---------------------------------------------------


@pytest.mark.parametrize("script", ["session-start.sh", "stop.sh", "user-prompt-submit.sh"])
def test_launcher_returns_with_stdin_pipe_left_open(plugin: Plugin, script: str) -> None:
    """The hook runner keeps the write end of stdin open; nothing may block on it."""
    plugin.mark_ready()
    process = subprocess.Popen(
        ["bash", str(plugin.root / "hooks" / script), "claude"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=plugin.env,
        cwd=str(plugin.root),
        text=True,
    )
    started = time.time()
    try:
        returncode = process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        pytest.fail(f"{script} did not exit within 6s with an open stdin pipe")
    elapsed = time.time() - started
    process.stdin.close()
    stdout = process.stdout.read()
    process.stdout.close()
    process.stderr.close()

    assert elapsed < 6
    assert returncode == 0
    assert stdout.strip() == "" or isinstance(json.loads(stdout), dict)


def test_failing_cli_leaves_stdout_empty_or_json(plugin: Plugin) -> None:
    """After `exec` the exit code is Python's; stdout must still be host-safe."""
    plugin.mark_ready()

    result = plugin.run("stop.sh", "claude", stdin="{}", extra_env={"FAKE_RC": "1", "FAKE_STDOUT": ""})

    assert result.stdout == ""
    assert result.returncode == 1  # the launcher exec'd; this is Python's code


def test_cli_stdout_is_forwarded_verbatim(plugin: Plugin) -> None:
    plugin.mark_ready()
    payload = '{"systemMessage": "[memsearch-mini v0.1.0] ready"}'

    result = plugin.run("session-start.sh", "claude", stdin="{}", extra_env={"FAKE_STDOUT": payload})

    assert result.returncode == 0
    assert json.loads(result.stdout)["systemMessage"] == "[memsearch-mini v0.1.0] ready"


@pytest.mark.parametrize("script", ["session-start.sh", "stop.sh", "user-prompt-submit.sh"])
def test_kill_switch_never_reaches_uv(plugin: Plugin, script: str) -> None:
    plugin.mark_ready()

    result = plugin.run(script, "claude", stdin="{}", extra_env={"MEMSEARCH_MINI_DISABLE": "1"})

    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    assert plugin.uv_calls() == []
    assert plugin.cli_calls() == []


@pytest.mark.parametrize("script", ["session-start.sh", "stop.sh", "user-prompt-submit.sh"])
def test_uv_missing_is_reported_and_nothing_else_runs(plugin: Plugin, script: str) -> None:
    result = plugin.run(script, "claude", stdin="{}", extra_env={"PATH": "/usr/bin:/bin"})

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert UV_MISSING in payload["systemMessage"]
    assert "https://docs.astral.sh/uv/" in payload["systemMessage"]
    assert plugin.cli_calls() == []


# --- runtime not ready --------------------------------------------------------


def test_session_start_spawns_exactly_one_detached_sync(plugin: Plugin) -> None:
    result = plugin.run("session-start.sh", "claude", stdin="{}")

    assert result.returncode == 0
    assert INSTALLING in json.loads(result.stdout)["systemMessage"]

    assert plugin.wait_for(lambda: len(plugin.sync_calls()) >= 1), "no detached sync was spawned"
    # The detached child also runs session-start once, so the model download and
    # the first index start during this session instead of the next one.
    assert plugin.wait_for(lambda: plugin.cli_calls() == ["hook session-start --platform claude"])
    time.sleep(0.5)
    assert plugin.sync_calls() == [f"sync --project {plugin.root} --frozen --extra onnx"]
    assert plugin.cli_calls() == ["hook session-start --platform claude"]


def test_detached_sync_carries_the_platform(plugin: Plugin) -> None:
    plugin.run("session-start.sh", "codex", stdin="{}")

    assert plugin.wait_for(lambda: plugin.cli_calls() == ["hook session-start --platform codex"])


@pytest.mark.parametrize("script", ["stop.sh", "user-prompt-submit.sh"])
def test_stop_and_prompt_submit_are_noops_until_ready(plugin: Plugin, script: str) -> None:
    result = plugin.run(script, "claude", stdin="{}")

    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    time.sleep(0.3)
    assert plugin.uv_calls() == []
    assert plugin.cli_calls() == []


def test_uv_lock_newer_than_stamp_means_not_ready(plugin: Plugin) -> None:
    plugin.mark_ready()
    future = time.time() + 60
    os.utime(plugin.root / "uv.lock", (future, future))

    result = plugin.run("session-start.sh", "claude", stdin="{}")

    assert INSTALLING in json.loads(result.stdout)["systemMessage"]
    # At least one: the artificial future mtime keeps the environment "not
    # ready" even after the sync, so the detached child's own bin/memsearch-mini
    # call syncs a second time. A real sync stamps itself newer than uv.lock.
    assert plugin.wait_for(lambda: len(plugin.sync_calls()) >= 1)


def test_pyproject_newer_than_stamp_means_not_ready(plugin: Plugin) -> None:
    plugin.mark_ready()
    future = time.time() + 60
    os.utime(plugin.root / "pyproject.toml", (future, future))

    result = plugin.run("session-start.sh", "claude", stdin="{}")

    assert INSTALLING in json.loads(result.stdout)["systemMessage"]


def test_missing_stamp_means_not_ready(plugin: Plugin) -> None:
    plugin.mark_ready()
    (plugin.venv / ".memsearch-mini-synced").unlink()

    result = plugin.run("session-start.sh", "claude", stdin="{}")

    assert INSTALLING in json.loads(result.stdout)["systemMessage"]


# --- common.sh library --------------------------------------------------------


def test_sync_detached_returns_immediately_while_the_child_runs(plugin: Plugin) -> None:
    started = time.time()
    result = plugin.bash("sync_detached claude", extra_env={"FAKE_SYNC_SLEEP": "2"})
    elapsed = time.time() - started

    assert result.returncode == 0
    # The child is still sleeping inside `uv sync` when the parent is back.
    assert elapsed < 2.0
    assert not (plugin.venv / "bin" / "memsearch-mini").exists()
    assert plugin.wait_for(lambda: (plugin.venv / "bin" / "memsearch-mini").exists(), timeout=20)


def test_sync_lock_makes_the_loser_give_up_at_once(plugin: Plugin) -> None:
    lock = plugin.venv.with_name(plugin.venv.name + ".lock")
    lock.mkdir(parents=True)

    result = plugin.bash("sync_now; echo rc=$?")

    assert "rc=1" in result.stdout
    assert plugin.sync_calls() == []
    assert lock.exists()  # the loser must not remove a lock it does not hold


def test_sync_now_reports_failure_and_releases_the_lock(plugin: Plugin) -> None:
    result = plugin.bash("sync_now; echo rc=$?", extra_env={"FAKE_SYNC_RC": "3"})

    assert "rc=3" in result.stdout
    assert not plugin.venv.with_name(plugin.venv.name + ".lock").exists()
    assert not (plugin.venv / ".memsearch-mini-synced").exists()


def test_sync_now_records_the_checkout_that_owns_the_environment(plugin: Plugin) -> None:
    """The sidecar is what lets a later session tell an orphan from a live environment."""
    result = plugin.bash("sync_now; echo rc=$?")

    assert "rc=0" in result.stdout
    assert plugin.root_file.read_text(encoding="utf-8").strip() == str(plugin.root)


def test_sync_now_records_the_checkout_even_when_the_sync_fails(plugin: Plugin) -> None:
    """Provenance is unconditional: a half-built environment is exactly what gets orphaned."""
    result = plugin.bash("sync_now; echo rc=$?", extra_env={"FAKE_SYNC_RC": "3"})

    assert "rc=3" in result.stdout
    assert plugin.root_file.read_text(encoding="utf-8").strip() == str(plugin.root)


def test_sync_log_never_lands_inside_the_venv(plugin: Plugin) -> None:
    """uv refuses a venv directory that exists without an interpreter, so the
    log and the lock are siblings of the venv, never inside it."""
    plugin.run("session-start.sh", "claude", stdin="{}")
    assert plugin.wait_for(lambda: len(plugin.sync_calls()) == 1)

    assert plugin.sync_log.exists()
    assert not (plugin.venv / "sync.log").exists()


def test_sync_log_is_truncated_when_it_grows_past_1mb(plugin: Plugin) -> None:
    plugin.sync_log.write_text("x" * (1024 * 1024 + 10), encoding="utf-8")

    plugin.bash("_prepare_log")

    assert plugin.sync_log.stat().st_size == 0


def test_prepare_log_is_silent_before_the_log_exists(plugin: Plugin) -> None:
    """First run ever: no log yet. bash's redirect error must not leak."""
    assert not plugin.sync_log.exists()

    result = plugin.bash("_prepare_log; echo rc=$?")

    assert "rc=0" in result.stdout
    assert result.stderr == ""


def test_extras_args_defaults_to_onnx_only(plugin: Plugin) -> None:
    result = plugin.bash("extras_args")

    assert result.stdout.strip() == "--extra onnx"


@pytest.mark.parametrize("provider", ["openai", "google", "voyage", "jina", "mistral", "ollama", "local"])
def test_extras_args_adds_the_configured_provider(plugin: Plugin, provider: str) -> None:
    Path(plugin.env["MEMSEARCH_MINI_CONFIG"]).write_text(
        f'[embedding]\nprovider = "{provider}"\nmodel = ""\n', encoding="utf-8"
    )

    result = plugin.bash("extras_args")

    assert result.stdout.strip() == f"--extra onnx --extra {provider}"


@pytest.mark.parametrize("value", ['"onnx"', '"nonsense"', "'openai'", "openai"])
def test_extras_args_handles_quotes_and_unknown_providers(plugin: Plugin, value: str) -> None:
    Path(plugin.env["MEMSEARCH_MINI_CONFIG"]).write_text(f"[embedding]\nprovider = {value}\n", encoding="utf-8")

    result = plugin.bash("extras_args")

    expected = "--extra onnx --extra openai" if "openai" in value else "--extra onnx"
    assert result.stdout.strip() == expected


def test_extras_args_reaches_uv_run(plugin: Plugin) -> None:
    # Built with the same extras the config asks for, so the launcher runs the
    # CLI instead of resyncing.
    plugin.mark_ready(extras="--extra onnx --extra openai")
    Path(plugin.env["MEMSEARCH_MINI_CONFIG"]).write_text('[embedding]\nprovider = "openai"\n', encoding="utf-8")

    plugin.run("session-start.sh", "claude", stdin="{}")

    assert "--extra onnx --extra openai" in plugin.uv_calls()[-1]


# --- bin/memsearch-mini ------------------------------------------------------------


def test_bin_memsearch_mini_syncs_when_the_runtime_is_missing(plugin: Plugin) -> None:
    result = subprocess.run(
        ["bash", str(plugin.root / "bin" / "memsearch-mini"), "search", "foo", "-k", "5", "--json"],
        capture_output=True,
        text=True,
        env=plugin.env,
        cwd=str(plugin.root),
        timeout=30,
    )

    assert result.returncode == 0
    assert len(plugin.sync_calls()) == 1
    assert plugin.cli_calls() == ["search foo -k 5 --json"]


def test_bin_memsearch_mini_sync_flag_is_blocking(plugin: Plugin) -> None:
    result = subprocess.run(
        ["bash", str(plugin.root / "bin" / "memsearch-mini"), "--sync"],
        capture_output=True,
        text=True,
        env=plugin.env,
        cwd=str(plugin.root),
        timeout=30,
    )

    assert result.returncode == 0
    assert "runtime ready" in result.stdout
    assert (plugin.venv / "bin" / "memsearch-mini").exists()
    assert (plugin.venv / ".memsearch-mini-synced").exists()
    assert plugin.cli_calls() == []


def test_bin_memsearch_mini_sync_failure_points_at_the_log(plugin: Plugin) -> None:
    result = subprocess.run(
        ["bash", str(plugin.root / "bin" / "memsearch-mini"), "--sync"],
        capture_output=True,
        text=True,
        env={**plugin.env, "FAKE_SYNC_RC": "2"},
        cwd=str(plugin.root),
        timeout=30,
    )

    assert result.returncode == 1
    assert str(plugin.sync_log) in result.stderr


def test_bin_memsearch_mini_without_uv_exits_one(plugin: Plugin) -> None:
    result = subprocess.run(
        ["bash", str(plugin.root / "bin" / "memsearch-mini"), "--version"],
        capture_output=True,
        text=True,
        env={**plugin.env, "PATH": "/usr/bin:/bin"},
        cwd=str(plugin.root),
        timeout=30,
    )

    assert result.returncode == 1
    assert "uv not found" in result.stderr


def test_symlinked_bin_memsearch_mini_resolves_the_plugin_root(plugin: Plugin, tmp_path: Path) -> None:
    plugin.mark_ready()
    link_dir = tmp_path / "elsewhere"
    link_dir.mkdir()
    link = link_dir / "memsearch-mini"
    link.symlink_to(plugin.root / "bin" / "memsearch-mini")

    result = subprocess.run(
        ["bash", str(link), "stats"],
        capture_output=True,
        text=True,
        env=plugin.env,
        cwd=str(tmp_path),
        timeout=30,
    )

    assert result.returncode == 0
    assert f"--project {plugin.root}" in plugin.uv_calls()[-1]
    assert plugin.cli_calls() == ["stats"]


def test_bin_memsearch_mini_exports_the_plugin_root(plugin: Plugin) -> None:
    plugin.mark_ready()
    probe = plugin.venv / "bin" / "memsearch-mini"
    probe.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$MEMSEARCH_MINI_PLUGIN_ROOT" > "$FAKE_MEMSEARCH_MINI_LOG"\n',
        encoding="utf-8",
    )
    probe.chmod(0o755)

    subprocess.run(
        ["bash", str(plugin.root / "bin" / "memsearch-mini"), "stats"],
        capture_output=True,
        text=True,
        env=plugin.env,
        cwd=str(plugin.root),
        timeout=30,
    )

    assert plugin.cli_log.read_text(encoding="utf-8").strip() == str(plugin.root)


# --- one runtime home ---------------------------------------------------------


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_default_environment_lives_under_memsearch_mini_home(plugin: Plugin) -> None:
    result = plugin.bash(
        'printf "%s\\n%s\\n" "$VENV" "$UV_PROJECT_ENVIRONMENT"',
        drop_env=("UV_PROJECT_ENVIRONMENT",),
    )

    venv, exported = result.stdout.split()
    # Keyed by checkout path, so two clones never share one environment.
    assert venv == str(plugin.default_venv)
    # Exported, or `uv sync` and `uv run` would fall back to $ROOT/.venv.
    assert exported == str(plugin.default_venv)
    assert str(plugin.root) not in venv


def test_default_memsearch_mini_home_is_dot_memsearch_mini_in_home(plugin: Plugin) -> None:
    result = plugin.bash('printf "%s\\n" "$MEMSEARCH_MINI_HOME"', drop_env=("MEMSEARCH_MINI_HOME",))

    assert result.stdout.strip() == str(plugin.home / ".memsearch-mini")


def test_caches_are_redirected_into_memsearch_mini_home(plugin: Plugin) -> None:
    plugin.mark_ready()

    plugin.run("session-start.sh", "claude", stdin="{}")

    seen = plugin.uv_env()
    assert seen["MEMSEARCH_MINI_HOME"] == str(plugin.ms_home)
    assert seen["UV_CACHE_DIR"] == str(plugin.ms_home / "uv-cache")
    assert seen["UV_PYTHON_INSTALL_DIR"] == str(plugin.ms_home / "python")
    assert seen["HF_HOME"] == str(plugin.ms_home / "models")


def test_user_set_cache_directories_win(plugin: Plugin, tmp_path: Path) -> None:
    plugin.mark_ready()
    custom_cache = tmp_path / "my-uv-cache"
    custom_models = tmp_path / "my-models"

    plugin.run(
        "session-start.sh",
        "claude",
        stdin="{}",
        extra_env={"UV_CACHE_DIR": str(custom_cache), "HF_HOME": str(custom_models)},
    )

    seen = plugin.uv_env()
    assert seen["UV_CACHE_DIR"] == str(custom_cache)
    assert seen["HF_HOME"] == str(custom_models)
    assert seen["UV_PYTHON_INSTALL_DIR"] == str(plugin.ms_home / "python")


def test_extras_args_reads_config_from_memsearch_mini_home(plugin: Plugin) -> None:
    config = plugin.ms_home / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[embedding]\nprovider = "voyage"\n', encoding="utf-8")

    result = plugin.bash("extras_args", drop_env=("MEMSEARCH_MINI_CONFIG",))

    assert result.stdout.strip() == "--extra onnx --extra voyage"


def test_full_install_cycle_leaves_the_checkout_untouched(plugin: Plugin) -> None:
    before = snapshot(plugin.root)

    result = plugin.run("session-start.sh", "claude", stdin="{}", drop_env=("UV_PROJECT_ENVIRONMENT",))

    assert INSTALLING in json.loads(result.stdout)["systemMessage"]
    assert plugin.wait_for(lambda: (plugin.default_venv / "bin" / "memsearch-mini").exists())
    assert plugin.wait_for(lambda: plugin.cli_calls() == ["hook session-start --platform claude"])
    # The venv, its log and its lock all landed under $MEMSEARCH_MINI_HOME.
    assert plugin.default_venv.with_name(plugin.default_venv.name + ".log").is_file()
    assert snapshot(plugin.root) == before


@pytest.mark.parametrize("script", ["session-start.sh", "stop.sh", "user-prompt-submit.sh"])
def test_ready_launcher_writes_nothing_into_the_checkout(plugin: Plugin, script: str) -> None:
    plugin.mark_ready()
    before = snapshot(plugin.root)

    plugin.run(script, "claude", stdin="{}")

    assert snapshot(plugin.root) == before


# --- provider changes trigger a resync ----------------------------------------


def test_sync_writes_the_extras_into_the_stamp(plugin: Plugin) -> None:
    Path(plugin.env["MEMSEARCH_MINI_CONFIG"]).write_text('[embedding]\nprovider = "jina"\n', encoding="utf-8")

    result = plugin.bash("sync_now; echo rc=$?")

    assert "rc=0" in result.stdout
    assert plugin.stamp.read_text(encoding="utf-8").strip() == "--extra onnx --extra jina"
    assert plugin.sync_calls() == [f"sync --project {plugin.root} --frozen --extra onnx --extra jina"]


def test_changing_the_provider_makes_the_runtime_not_ready(plugin: Plugin) -> None:
    plugin.mark_ready()  # built with --extra onnx
    Path(plugin.env["MEMSEARCH_MINI_CONFIG"]).write_text('[embedding]\nprovider = "openai"\n', encoding="utf-8")

    result = plugin.run("session-start.sh", "claude", stdin="{}")

    assert INSTALLING in json.loads(result.stdout)["systemMessage"]
    assert plugin.wait_for(lambda: len(plugin.sync_calls()) >= 1)
    assert plugin.sync_calls()[0].endswith("--extra onnx --extra openai")
    assert plugin.wait_for(lambda: plugin.stamp.read_text(encoding="utf-8").strip() == "--extra onnx --extra openai")


def test_a_stamp_that_matches_the_config_stays_ready(plugin: Plugin) -> None:
    plugin.mark_ready(extras="--extra onnx --extra openai")
    Path(plugin.env["MEMSEARCH_MINI_CONFIG"]).write_text('[embedding]\nprovider = "openai"\n', encoding="utf-8")

    result = plugin.run("session-start.sh", "claude", stdin="{}")

    assert result.stdout == "{}"
    assert plugin.cli_calls() == ["hook session-start --platform claude"]
    assert plugin.sync_calls() == []


def test_bin_memsearch_mini_resyncs_inline_after_a_provider_change(plugin: Plugin) -> None:
    plugin.mark_ready()
    Path(plugin.env["MEMSEARCH_MINI_CONFIG"]).write_text('[embedding]\nprovider = "mistral"\n', encoding="utf-8")

    result = subprocess.run(
        ["bash", str(plugin.root / "bin" / "memsearch-mini"), "stats"],
        capture_output=True,
        text=True,
        env=plugin.env,
        cwd=str(plugin.root),
        timeout=30,
    )

    assert result.returncode == 0
    assert plugin.sync_calls() == [f"sync --project {plugin.root} --frozen --extra onnx --extra mistral"]
    assert plugin.cli_calls() == ["stats"]
    assert plugin.stamp.read_text(encoding="utf-8").strip() == "--extra onnx --extra mistral"
