"""Tests for memsearch_mini.hooks — payload reading, the session-start status and
recent-memory injection, and both stop pipelines. PATH is sanitized so no test
can reach the real ``claude``/``codex`` binaries, and every write lands in
tmp_path."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from memsearch_mini import config, hooks

FILLER = "x" * 180


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    scratch = tmp_path / "tmp"
    scratch.mkdir(exist_ok=True)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setenv("MEMSEARCH_MINI_CONFIG", str(tmp_path / "config.toml"))
    # No test may reach the real agent CLIs; git stays available on purpose.
    monkeypatch.setenv("PATH", os.pathsep.join([str(fakebin), "/usr/bin", "/bin"]))
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    for name in (
        "MEMSEARCH_MINI_DIR",
        "CLAUDE_PROJECT_DIR",
        "MEMSEARCH_MINI_DISABLE",
        "MEMSEARCH_MINI_IN_STOP_WORKER",
        "MEMSEARCH_MINI_PLUGIN_ROOT",
        "MEMSEARCH_MINI_SUMMARY_MAX_CHARS",
        "CODEX_HOME",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def project(tmp_path, monkeypatch):
    directory = tmp_path / "project"
    (directory / ".memsearch-mini" / "memory").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(directory))
    return directory


@pytest.fixture
def spawns(monkeypatch):
    """Records every detached child instead of starting one."""
    calls: list[tuple[list[str], str, dict]] = []
    monkeypatch.setattr(hooks, "_spawn_detached", lambda argv, cwd, env: calls.append((argv, str(cwd), env)))
    return calls


def _invoke(args, payload=None):
    runner = CliRunner()
    stdin = "" if payload is None else json.dumps(payload)
    return runner.invoke(hooks.hook, args, input=stdin, catch_exceptions=False)


def _json(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _fake_bin(tmp_path, name: str, body: str) -> Path:
    script = tmp_path / "fakebin" / name
    script.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


def _memory(project: Path) -> Path:
    return project / ".memsearch-mini" / "memory"


def _make_index(project: Path, *, chunks: int = 3, last_index_at: float | None = None) -> Path:
    db_path = project / ".memsearch-mini" / "index.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, chunk_id TEXT)")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.executemany("INSERT INTO chunks (chunk_id) VALUES (?)", [(f"c{i}",) for i in range(chunks)])
    if last_index_at is not None:
        conn.execute("INSERT INTO meta VALUES ('last_index_at', ?)", (str(last_index_at),))
    conn.commit()
    conn.close()
    return db_path


def _claude_transcript(path: Path, *, uuid: str = "turn-a", lines: int = 3) -> Path:
    rows = [
        {"type": "system", "message": {"content": "start"}},
        {"type": "user", "uuid": uuid, "message": {"content": "Summarize this session"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "I explained the hook."}]}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows[:lines]) + "\n", encoding="utf-8")
    return path


def _codex_rollout(path: Path) -> Path:
    rows = [
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Check the journal"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Done."}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


# --- read_payload ------------------------------------------------------------


def test_read_payload_returns_before_the_writer_closes():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'{"cwd": "/somewhere", "session_id": "s1"}')
    started = time.monotonic()
    with os.fdopen(read_fd, "rb") as stream:
        payload = hooks.read_payload(stream)
    elapsed = time.monotonic() - started
    os.close(write_fd)

    assert payload == {"cwd": "/somewhere", "session_id": "s1"}
    assert elapsed < 0.3  # the host keeps the pipe open: EOF must not be awaited


def test_read_payload_returns_an_empty_object_immediately():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"{}")
    started = time.monotonic()
    with os.fdopen(read_fd, "rb") as stream:
        payload = hooks.read_payload(stream)
    elapsed = time.monotonic() - started
    os.close(write_fd)

    assert payload == {}
    assert elapsed < 0.3


def test_read_payload_accepts_a_split_write():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'{"cwd": ')
    with os.fdopen(read_fd, "rb") as stream:
        os.write(write_fd, b'"/late"}')
        payload = hooks.read_payload(stream)
    os.close(write_fd)

    assert payload == {"cwd": "/late"}


def test_read_payload_handles_devnull_garbage_and_silence():
    with open(os.devnull, "rb") as stream:
        assert hooks.read_payload(stream) == {}

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"not json at all")
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as stream:
        assert hooks.read_payload(stream) == {}

    read_fd, write_fd = os.pipe()
    started = time.monotonic()
    with os.fdopen(read_fd, "rb") as stream:
        assert hooks.read_payload(stream, timeout=0.2) == {}
    assert time.monotonic() - started < 1.0
    os.close(write_fd)


def test_read_payload_falls_back_to_read_without_a_descriptor():
    assert hooks.read_payload(io.StringIO('{"cwd": "/x"}')) == {"cwd": "/x"}
    assert hooks.read_payload(io.StringIO("[1, 2]")) == {}


def test_read_payload_reads_the_cli_runner_stdin(project, spawns):
    payload = _json(_invoke(["session-start", "--platform", "claude"], {"cwd": str(project)}))

    assert str(_memory(project)) in payload["systemMessage"]


# --- project resolution ------------------------------------------------------


def test_resolve_project_dir_precedence(tmp_path, monkeypatch):
    existing = tmp_path / "from-env"
    existing.mkdir()
    payload_cwd = tmp_path / "from-payload"
    payload_cwd.mkdir()

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(existing))
    assert hooks.resolve_project_dir({"cwd": str(payload_cwd)}) == existing

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "does-not-exist"))
    assert hooks.resolve_project_dir({"cwd": str(payload_cwd)}) == payload_cwd

    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    monkeypatch.chdir(payload_cwd)
    assert hooks.resolve_project_dir({}) == payload_cwd


def test_resolve_project_dir_climbs_to_the_git_root(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src" / "deep").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)

    assert hooks.resolve_project_dir({"cwd": str(repo / "src" / "deep")}) == repo


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit_something(repo):
    (repo / "README").write_text("x", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")


def test_resolve_project_dir_maps_a_linked_worktree_to_the_primary_checkout(tmp_path):
    """Upstream #717: one memory per repository, not one per worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit_something(repo)
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature")

    assert hooks.resolve_project_dir({"cwd": str(worktree)}) == repo
    assert hooks.resolve_project_dir({"cwd": str(repo)}) == repo


def test_resolve_project_dir_keeps_a_submodule_separate(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    _git(inner, "init", "-q")
    _commit_something(inner)
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    _commit_something(outer)
    _git(outer, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "lib")

    assert hooks.resolve_project_dir({"cwd": str(outer / "lib")}) == outer / "lib"


def test_memsearch_mini_and_memory_dir(tmp_path, monkeypatch):
    assert hooks.memsearch_mini_dir(tmp_path) == tmp_path / ".memsearch-mini"
    assert hooks.memory_dir(tmp_path) == tmp_path / ".memsearch-mini" / "memory"

    monkeypatch.setenv("MEMSEARCH_MINI_DIR", str(tmp_path / "elsewhere"))
    assert hooks.memory_dir(tmp_path) == tmp_path / "elsewhere" / "memory"


# --- session-start -----------------------------------------------------------


def test_session_start_without_memory_injects_nothing(project, spawns):
    payload = _json(_invoke(["session-start", "--platform", "claude"]))

    assert "hookSpecificOutput" not in payload
    assert payload["systemMessage"].startswith("[memsearch-mini v")
    assert "embedding: onnx/gpahal/bge-m3-onnx-int8" in payload["systemMessage"]
    assert f"memory: {_memory(project)}" in payload["systemMessage"]
    assert _memory(project).is_dir()


def test_session_start_is_a_no_op_when_disabled(project, spawns, monkeypatch, tmp_path):
    monkeypatch.setenv("MEMSEARCH_MINI_DISABLE", "1")
    fresh = tmp_path / "untouched"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(fresh))

    assert _json(_invoke(["session-start", "--platform", "codex"])) == {}
    assert not fresh.exists()
    assert spawns == []


def test_session_start_bootstraps_the_config_once(project, spawns):
    config_file = config.config_path()
    assert not config_file.exists()

    _invoke(["session-start", "--platform", "claude"])

    assert config.load().embedding.provider == "onnx"
    assert 'provider = "onnx"' in config_file.read_text(encoding="utf-8")

    config_file.write_text('[embedding]\nprovider = "ollama"\n', encoding="utf-8")
    payload = _json(_invoke(["session-start", "--platform", "claude"]))

    assert config_file.read_text(encoding="utf-8") == '[embedding]\nprovider = "ollama"\n'
    assert "embedding: ollama/nomic-embed-text" in payload["systemMessage"]


def test_session_start_reports_a_missing_api_key_and_skips_indexing(project, spawns):
    config.config_path().write_text('[embedding]\nprovider = "openai"\n', encoding="utf-8")
    (_memory(project) / "2026-08-19.md").write_text("## Session 10:00\n### 10:00\n- Something.\n", encoding="utf-8")

    payload = _json(_invoke(["session-start", "--platform", "claude"]))

    assert "ERROR: OPENAI_API_KEY not set — memory search disabled" in payload["systemMessage"]
    assert "Tip: memsearch-mini config set embedding.provider onnx" in payload["systemMessage"]
    assert "hookSpecificOutput" not in payload
    assert spawns == []


def test_session_start_reports_a_missing_onnxruntime(project, spawns, monkeypatch):
    monkeypatch.setattr(hooks.importlib.util, "find_spec", lambda name: None)

    payload = _json(_invoke(["session-start", "--platform", "claude"]))

    assert "ERROR: onnxruntime not installed — memory search disabled" in payload["systemMessage"]
    assert "Tip: uv sync --extra onnx" in payload["systemMessage"]
    assert spawns == []


def test_session_start_reindexes_when_the_journal_is_newer(project, spawns):
    _make_index(project, chunks=7, last_index_at=time.time() - 600)
    (_memory(project) / "2026-08-19.md").write_text("- fresh\n", encoding="utf-8")

    payload = _json(_invoke(["session-start", "--platform", "claude"]))

    assert "index: 7 chunks, updated " in payload["systemMessage"]
    assert "— stale — reindexing in background" in payload["systemMessage"]
    assert [call[0] for call in spawns] == [hooks._index_argv(_memory(project))]
    assert spawns[0][1] == str(project)
    assert spawns[0][2]["MEMSEARCH_MINI_DISABLE"] == "1"


def test_session_start_keeps_quiet_when_the_index_is_fresh(project, spawns):
    (_memory(project) / "2026-08-19.md").write_text("- old\n", encoding="utf-8")
    _make_index(project, chunks=4, last_index_at=time.time() + 600)

    payload = _json(_invoke(["session-start", "--platform", "claude"]))

    assert "index: 4 chunks, updated " in payload["systemMessage"]
    assert "stale" not in payload["systemMessage"]
    assert spawns == []


def test_session_start_reports_a_missing_index(project, spawns):
    payload = _json(_invoke(["session-start", "--platform", "codex"]))

    assert "index: not built yet — building in background" in payload["systemMessage"]
    assert len(spawns) == 1


def test_session_start_detaches_the_reindex(project, monkeypatch):
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return None

    monkeypatch.setattr(hooks.subprocess, "Popen", fake_popen)

    _invoke(["session-start", "--platform", "claude"])

    argv, kwargs = next(call for call in calls if call[0][0] == sys.executable)
    assert argv == [sys.executable, "-m", "memsearch_mini", "index", str(_memory(project)), "--skip-if-locked"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    # Output goes to a plain log file under $MEMSEARCH_MINI_HOME, never to the hook's own pipes.
    assert kwargs["stdout"] is kwargs["stderr"]
    assert kwargs["stdout"].name == str(config.home_dir() / "index.log")
    kwargs["stdout"].close()
    assert kwargs["cwd"] == str(project)
    assert kwargs["env"]["MEMSEARCH_MINI_DISABLE"] == "1"


# --- recent-memory preview ---------------------------------------------------


def _context(project, journals: dict[str, str]) -> str:
    for name, text in journals.items():
        (_memory(project) / name).write_text(text, encoding="utf-8")
    payload = _json(_invoke(["session-start", "--platform", "claude"]))
    return payload.get("hookSpecificOutput", {}).get("additionalContext", "")


def test_recent_memory_skips_sessions_without_bullets(project, spawns):
    context = _context(
        project,
        {
            "2026-01-01.md": (
                "# 2026-01-01\n\n## Session 09:00\n\n## Session 09:01\n\n### 09:01\n"
                "- User discussed a useful migration note.\n\n## Session 09:02\n"
            ),
            "zzz-scratch.md": "# Scratch\n\n## Session 10:00\n\n### 10:00\n- Scratch content.\n",
        },
    )

    assert context.startswith("# Recent Memory\n\n## 2026-01-01.md\n")
    assert "- User discussed a useful migration note." in context
    assert "## Session 09:01" in context
    assert "Scratch content." not in context  # only YYYY-MM-DD.md journals count
    assert "Session 09:00" not in context
    assert "Session 09:02" not in context
    assert len(context.encode("utf-8")) <= hooks.RECENT_MEMORY_MAX_BYTES


def test_recent_memory_keeps_the_latest_entries_within_budget(project, spawns):
    journal = "\n".join(
        [
            "# 2026-08-19",
            "",
            "## Session 09:00",
            "### 09:00",
            "- OLDEST_ENTRY",
            *[f"- {FILLER}-{index}" for index in range(30)],
            "",
            "## Session 17:00",
            "### 17:00",
            "- LATEST_ENTRY",
            "",
        ]
    )

    context = _context(project, {"2026-08-19.md": journal})

    assert "LATEST_ENTRY" in context
    assert "OLDEST_ENTRY" not in context
    assert len(context.encode("utf-8")) <= hooks.RECENT_MEMORY_MAX_BYTES


def test_recent_memory_prioritizes_the_newest_journal(project, spawns):
    newest = "\n".join(
        [
            "# 2026-08-19",
            "",
            "## Session 17:00",
            "### 17:00",
            *[f"- {FILLER}-{index}" for index in range(20)],
            "- TODAY_LATEST_MARKER",
            "",
        ]
    )
    older = f"# 2026-08-18\n\n## Session 17:00\n### 17:00\n- OLD_DAY_MARKER {FILLER}\n"

    context = _context(project, {"2026-08-18.md": older, "2026-08-19.md": newest})

    assert "TODAY_LATEST_MARKER" in context
    assert "OLD_DAY_MARKER" not in context
    assert len(context.encode("utf-8")) <= hooks.RECENT_MEMORY_MAX_BYTES


def test_recent_memory_injects_both_journals_when_they_fit(project, spawns):
    context = _context(
        project,
        {
            "2026-08-17.md": "## Session 08:00\n### 08:00\n- IGNORED_THIRD_JOURNAL\n",
            "2026-08-18.md": "## Session 16:00\n### 16:00\n- OLDER_MARKER\n",
            "2026-08-19.md": "## Session 17:00\n### 17:00\n- NEWER_MARKER\n",
        },
    )

    assert context == (
        "# Recent Memory\n\n"
        "## 2026-08-19.md\n## Session 17:00\n### 17:00\n- NEWER_MARKER\n\n"
        "## 2026-08-18.md\n## Session 16:00\n### 16:00\n- OLDER_MARKER\n\n"
    )
    assert "IGNORED_THIRD_JOURNAL" not in context  # only the two newest journals
    assert len(context.encode("utf-8")) <= hooks.RECENT_MEMORY_MAX_BYTES


def test_recent_memory_stops_when_the_newest_entry_exceeds_the_budget(project, spawns):
    newest = f"# 2026-08-19\n\n## Session 17:00\n### 17:00\n- NEWEST_OVERSIZED_ENTRY {'x' * 2000}\n"
    older = "# 2026-08-18\n\n## Session 16:00\n### 16:00\n- OLD_DAY_MARKER\n"

    context = _context(project, {"2026-08-18.md": older, "2026-08-19.md": newest})

    assert context == ""  # no silent fallback to an older, less relevant journal


def test_recent_memory_trims_multibyte_text_on_line_boundaries(project, spawns):
    journal = "\n".join(
        [
            "# 2026-08-19",
            "",
            "## Session 17:00",
            "### 17:00",
            *[f"- 用户讨论了新的检索策略 需要保留完整中文内容 {index}" for index in range(50)],
            "- 最新记录: 需要优先保存",
            "",
        ]
    )

    context = _context(project, {"2026-08-19.md": journal})

    assert "最新记录" in context
    assert "�" not in context
    assert len(context.encode("utf-8")) <= hooks.RECENT_MEMORY_MAX_BYTES


def test_tail_within_bytes_counts_utf8_bytes():
    lines = ["日本語のとても長い行です", "short", "最後"]

    assert hooks._tail_within_bytes(lines, 1800) == lines
    assert hooks._tail_within_bytes(lines, 13) == ["short", "最後"]  # 6 + 5 bytes, one newline each
    assert hooks._tail_within_bytes(lines, 12) == ["最後"]  # "short" no longer fits whole
    assert hooks._tail_within_bytes(lines, 6) == []  # not even the newest line fits


# --- stop guards -------------------------------------------------------------


def test_stop_guards_return_an_empty_object(project, spawns, tmp_path, monkeypatch):
    transcript = _claude_transcript(tmp_path / "session-a.jsonl")
    payload = {"transcript_path": str(transcript), "session_id": "session-a"}

    monkeypatch.setenv("MEMSEARCH_MINI_DISABLE", "1")
    assert _json(_invoke(["stop", "--platform", "claude"], payload)) == {}
    monkeypatch.delenv("MEMSEARCH_MINI_DISABLE")

    monkeypatch.setenv("MEMSEARCH_MINI_IN_STOP_WORKER", "1")
    assert _json(_invoke(["stop", "--platform", "codex"], payload)) == {}
    monkeypatch.delenv("MEMSEARCH_MINI_IN_STOP_WORKER")

    assert _json(_invoke(["stop", "--platform", "claude"], {**payload, "stop_hook_active": True})) == {}
    assert _json(_invoke(["stop", "--platform", "claude"], {})) == {}
    assert _json(_invoke(["stop", "--platform", "claude"], {"transcript_path": str(tmp_path / "gone.jsonl")})) == {}

    short = _claude_transcript(tmp_path / "short.jsonl", lines=2)
    assert _json(_invoke(["stop", "--platform", "claude"], {"transcript_path": str(short)})) == {}

    config.config_path().write_text("[claude]\nsummarize_enabled = false\n", encoding="utf-8")
    assert _json(_invoke(["stop", "--platform", "claude"], payload)) == {}

    config.config_path().write_text('[embedding]\nprovider = "openai"\n', encoding="utf-8")
    assert _json(_invoke(["stop", "--platform", "claude"], payload)) == {}
    config.config_path().unlink()

    userless = tmp_path / "userless.jsonl"
    userless.write_text('{"type": "assistant"}\n{"type": "system"}\n{"type": "system"}\n', encoding="utf-8")
    assert _json(_invoke(["stop", "--platform", "claude"], {"transcript_path": str(userless)})) == {}

    assert list(_memory(project).glob("*.md")) == []
    assert spawns == []


def test_codex_stop_without_content_writes_nothing(project, spawns):
    assert _json(_invoke(["stop", "--platform", "codex"], {"session_id": "s1"})) == {}
    assert list(_memory(project).glob("*.md")) == []
    assert spawns == []


# --- stop: Claude ------------------------------------------------------------


def test_claude_stop_summarizes_and_appends(project, spawns, tmp_path):
    _fake_bin(tmp_path, "claude", 'if [ "${1:-}" = "--help" ]; then echo usage; exit 0; fi\necho "- A summary."\n')
    transcript = _claude_transcript(tmp_path / "session-a.jsonl", uuid="turn-a")

    payload = _json(_invoke(["stop", "--platform", "claude"], {"transcript_path": str(transcript)}))

    assert payload == {"systemMessage": "[memsearch-mini] turn captured"}
    journals = list(_memory(project).glob("*.md"))
    text = journals[0].read_text(encoding="utf-8")
    assert text.startswith("\n## Session ")
    assert f"<!-- session:session-a turn:turn-a transcript:{transcript} -->" in text
    assert text.endswith("- A summary.\n\n")
    assert [call[0] for call in spawns] == [hooks._index_argv(_memory(project))]


def test_claude_stop_records_the_failure_instead_of_the_transcript(project, spawns, tmp_path):
    _fake_bin(tmp_path, "claude", "exit 3\n")
    transcript = _claude_transcript(tmp_path / "session-a.jsonl")

    payload = _json(_invoke(["stop", "--platform", "claude"], {"transcript_path": str(transcript)}))

    assert payload == {
        "systemMessage": "[memsearch-mini] turn recorded without a summary (summarizer exited with status 3)"
    }
    text = next(iter(_memory(project).glob("*.md"))).read_text(encoding="utf-8")
    assert "- Memory summary unavailable: summarizer exited with status 3;" in text
    assert "Summarize this session" not in text  # never persist raw transcript text


def test_claude_stop_never_journals_a_rate_limit_notice(project, spawns, tmp_path):
    """Upstream #527: prose with exit 0 (rate limit, refusal) is a failure, not a summary."""
    _fake_bin(tmp_path, "claude", 'echo "You\'ve hit your limit. Resets at 11am."\n')
    transcript = _claude_transcript(tmp_path / "session-a.jsonl")

    _invoke(["stop", "--platform", "claude"], {"transcript_path": str(transcript)})

    text = next(iter(_memory(project).glob("*.md"))).read_text(encoding="utf-8")
    assert "- Memory summary unavailable: summarizer returned no bullet points;" in text
    assert "hit your limit" not in text


def test_recent_memory_includes_suffixed_journals(project, spawns):
    context = _context(project, {"2026-03-02-laptop.md": "## Session 09:00\n### 09:00\n- from the laptop\n"})
    assert "from the laptop" in context
    assert "## 2026-03-02-laptop.md" in context


# --- stop: Codex two-phase ---------------------------------------------------


def _handoff(project, spawns, tmp_path) -> Path:
    rollout = _codex_rollout(tmp_path / "rollout-sess-9.jsonl")
    payload = _json(
        _invoke(
            ["stop", "--platform", "codex"],
            {"transcript_path": str(rollout), "session_id": "sess-9", "last_assistant_message": "FINAL_ANSWER"},
        )
    )

    assert payload == {}
    argv, cwd, env = spawns[0]
    assert argv[:5] == [sys.executable, "-m", "memsearch_mini", "hook", "stop-worker"]
    assert env["MEMSEARCH_MINI_IN_STOP_WORKER"] == "1"
    assert cwd == str(project)
    return Path(argv[5])


def test_codex_stop_hands_a_work_file_to_the_detached_worker(project, spawns, tmp_path, monkeypatch):
    _fake_bin(tmp_path, "codex", 'echo "- Codex summarized the turn."\n')
    workfile = _handoff(project, spawns, tmp_path)

    assert workfile.name.startswith("memsearch-mini-stop.")
    assert workfile.suffix == ".json"
    assert workfile.parent == tmp_path / "tmp"
    work = json.loads(workfile.read_text(encoding="utf-8"))
    assert work["session_id"] == "sess-9"
    assert work["memory_dir"] == str(_memory(project))
    assert work["content"].startswith("=== Final exchange, authoritative for outcome ===")
    assert list(_memory(project).glob("*.md")) == []  # phase 1 never writes to the journal

    indexed: list[tuple[str, str]] = []
    monkeypatch.setattr(hooks, "_run_index", lambda memory, cwd: indexed.append((str(memory), str(cwd))))

    assert _json(_invoke(["stop-worker", str(workfile)])) == {}

    assert not workfile.exists()
    text = next(iter(_memory(project).glob("*.md"))).read_text(encoding="utf-8")
    assert f"<!-- session:sess-9 rollout:{tmp_path / 'rollout-sess-9.jsonl'} -->" in text
    assert "turn:" not in text
    assert text.endswith("- Codex summarized the turn.\n\n")
    assert indexed == [(str(_memory(project)), str(project))]


def test_codex_worker_removes_the_work_file_when_the_summarizer_fails(project, spawns, tmp_path, monkeypatch):
    workfile = _handoff(project, spawns, tmp_path)  # no codex binary on PATH
    monkeypatch.setattr(hooks, "_run_index", lambda memory, cwd: None)

    assert _json(_invoke(["stop-worker", str(workfile)])) == {}

    assert not workfile.exists()
    text = next(iter(_memory(project).glob("*.md"))).read_text(encoding="utf-8")
    assert "- User asked: Check the journal\n- Codex: FINAL_ANSWER\n" in text


def test_stop_worker_tolerates_a_missing_work_file(tmp_path, monkeypatch):
    indexed: list = []
    monkeypatch.setattr(hooks, "_run_index", lambda memory, cwd: indexed.append(memory))

    assert _json(_invoke(["stop-worker", str(tmp_path / "gone.json")])) == {}
    assert indexed == []


# --- stop: pending record and recovery ----------------------------------------


def _pending(project: Path) -> Path:
    return project / ".memsearch-mini" / "pending"


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _pending_record(project: Path, transcript: Path, *, uuid: str = "turn-a", now: str = "2026-03-02T09:15:00",
                    session: str = "session-a", age: float = 0.0) -> Path:  # fmt: skip
    _pending(project).mkdir(parents=True, exist_ok=True)
    path = _pending(project) / f"{session}-{uuid}.json"
    record = {
        "platform": "claude",
        "now": now,
        "session_id": session,
        "turn_uuid": uuid,
        "transcript_path": str(transcript),
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    _age(path, age)
    return path


def test_claude_stop_records_the_turn_before_summarizing_and_forgets_it_after(project, spawns, tmp_path, monkeypatch):
    transcript = _claude_transcript(tmp_path / "session-a.jsonl", uuid="turn-a")
    seen: list[dict] = []
    real = hooks._summarize_and_append

    def spy(cfg, platform, turn, memory, project_dir, now=None):
        seen.extend(json.loads(p.read_text(encoding="utf-8")) for p in _pending(project).glob("*.json"))
        real(cfg, platform, turn, memory, project_dir, now=now)

    monkeypatch.setattr(hooks, "_summarize_and_append", spy)
    _fake_bin(tmp_path, "claude", 'echo "- A summary."\n')

    payload = _json(_invoke(["stop", "--platform", "claude"], {"transcript_path": str(transcript)}))

    assert payload == {"systemMessage": "[memsearch-mini] turn captured"}
    assert [(r["session_id"], r["turn_uuid"], r["transcript_path"]) for r in seen] == [
        ("session-a", "turn-a", str(transcript))
    ]
    assert "content" not in seen[0] and "Summarize this session" not in json.dumps(seen[0])
    assert list(_pending(project).iterdir()) == []  # written, then discarded
    assert [call[0] for call in spawns] == [hooks._index_argv(_memory(project))]


def test_claude_stop_leaves_the_record_when_it_dies_mid_summary(project, spawns, tmp_path, monkeypatch):
    transcript = _claude_transcript(tmp_path / "session-a.jsonl", uuid="turn-a")

    def killed(*args, **kwargs):
        raise RuntimeError("SIGKILL stand-in")

    monkeypatch.setattr(hooks, "_summarize_and_append", killed)

    assert _json(_invoke(["stop", "--platform", "claude"], {"transcript_path": str(transcript)})) == {}

    assert [p.name for p in _pending(project).iterdir()] == ["session-a-turn-a.json"]
    assert list(_memory(project).glob("*.md")) == []


def test_fresh_records_are_left_to_their_running_hook(project, spawns, tmp_path):
    _make_index(project, last_index_at=time.time() + 600)  # nothing else to spawn
    transcript = _claude_transcript(tmp_path / "session-a.jsonl")
    _pending_record(project, transcript, age=hooks.PENDING_GRACE_SECONDS - 30)

    payload = _json(_invoke(["session-start", "--platform", "claude"], {}))

    assert spawns == []
    assert payload["systemMessage"].endswith(" | 1 turn(s) pending, recovery at the next turn end")


def test_session_start_and_stop_hand_stale_records_to_a_detached_worker(project, spawns, tmp_path):
    _make_index(project, last_index_at=time.time() + 600)  # nothing else to spawn
    transcript = _claude_transcript(tmp_path / "session-a.jsonl")
    _pending_record(project, transcript, age=hooks.PENDING_GRACE_SECONDS + 1)
    recover = [sys.executable, "-m", "memsearch_mini", "hook", "recover", str(project)]

    payload = _json(_invoke(["session-start", "--platform", "claude"], {}))
    assert [call[0] for call in spawns] == [recover]
    assert spawns[0][2]["MEMSEARCH_MINI_IN_STOP_WORKER"] == "1"
    assert payload["systemMessage"].endswith(" | recovering 1 earlier turn(s) in the background")

    spawns.clear()
    _fake_bin(tmp_path, "claude", 'echo "- A summary."\n')
    fresh = _claude_transcript(tmp_path / "session-b.jsonl", uuid="turn-b")
    payload = _json(_invoke(["stop", "--platform", "claude"], {"transcript_path": str(fresh)}))
    assert [call[0] for call in spawns] == [hooks._index_argv(_memory(project)), recover]
    assert payload == {
        "systemMessage": "[memsearch-mini] turn captured | recovering 1 earlier turn(s) in the background"
    }


def test_recover_journals_the_turn_into_the_day_it_happened(project, tmp_path, monkeypatch):
    _fake_bin(tmp_path, "claude", 'echo "- Recovered summary."\n')
    transcript = _claude_transcript(tmp_path / "session-a.jsonl", uuid="turn-a")
    _pending_record(project, transcript, now="2026-03-02T09:15:00", age=hooks.PENDING_GRACE_SECONDS + 1)
    indexed: list[str] = []
    monkeypatch.setattr(hooks, "_run_index", lambda memory, cwd: indexed.append(str(memory)))

    assert _json(_invoke(["recover", str(project)])) == {"systemMessage": "[memsearch-mini] recovered 1 turn(s)"}

    text = (_memory(project) / "2026-03-02.md").read_text(encoding="utf-8")
    assert text == (
        "\n## Session 09:15\n\n### 09:15\n"
        f"<!-- session:session-a turn:turn-a transcript:{transcript} -->\n- Recovered summary.\n\n"
    )
    assert list(_pending(project).iterdir()) == []
    assert indexed == [str(_memory(project))]


def test_recover_takes_the_recorded_turn_even_after_the_session_went_on(project, tmp_path, monkeypatch):
    """The transcript grew after the record (a resumed session): the recorded uuid wins."""
    _fake_bin(tmp_path, "claude", 'grep -o "First question\\|Second question" | head -n 1 | sed "s/^/- /"\n')
    rows = [
        {"type": "user", "uuid": "turn-a", "message": {"content": "First question"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "First answer."}]}},
        {"type": "user", "uuid": "turn-b", "message": {"content": "Second question"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Second answer."}]}},
    ]
    transcript = tmp_path / "session-a.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    _pending_record(project, transcript, uuid="turn-a", age=hooks.PENDING_GRACE_SECONDS + 1)
    monkeypatch.setattr(hooks, "_run_index", lambda memory, cwd: None)

    _invoke(["recover", str(project)])

    text = (_memory(project) / "2026-03-02.md").read_text(encoding="utf-8")
    assert "turn:turn-a" in text and "- First question" in text
    assert "turn-b" not in text and "Second" not in text


def test_recover_never_writes_a_turn_twice(project, tmp_path, monkeypatch):
    """The append landed, then the worker died before deleting its claim."""
    _fake_bin(tmp_path, "claude", 'echo "- Again."\n')
    transcript = _claude_transcript(tmp_path / "session-a.jsonl", uuid="turn-a")
    journal = _memory(project) / "2026-03-02.md"
    journal.write_text(
        f"\n## Session 09:15\n\n### 09:15\n<!-- session:session-a turn:turn-a transcript:{transcript} -->\n- Once.\n\n"
    )
    record = _pending_record(project, transcript, age=hooks.PENDING_GRACE_SECONDS + 1)
    stale_claim = record.with_name("session-a-turn-a.json.999.working")
    record.rename(stale_claim)
    _age(stale_claim, hooks.PENDING_WORKING_STALE_SECONDS + 1)
    indexed: list[str] = []
    monkeypatch.setattr(hooks, "_run_index", lambda memory, cwd: indexed.append(str(memory)))

    _invoke(["recover", str(project)])

    assert journal.read_text(encoding="utf-8").count("turn:turn-a") == 1
    assert list(_pending(project).iterdir()) == []
    assert indexed == []  # nothing new to index


def test_recover_drops_a_record_whose_transcript_is_gone(project, tmp_path, monkeypatch):
    _pending_record(project, tmp_path / "vanished.jsonl", age=hooks.PENDING_GRACE_SECONDS + 1)
    monkeypatch.setattr(hooks, "_run_index", lambda memory, cwd: None)

    assert _json(_invoke(["recover", str(project)])) == {"systemMessage": "[memsearch-mini] recovered 0 turn(s)"}

    assert list(_pending(project).iterdir()) == []
    assert list(_memory(project).glob("*.md")) == []


def test_recover_skips_a_record_another_worker_claimed(project, tmp_path, monkeypatch):
    transcript = _claude_transcript(tmp_path / "session-a.jsonl")
    record = _pending_record(project, transcript, age=hooks.PENDING_GRACE_SECONDS + 1)
    monkeypatch.setattr(hooks, "_pending_claim", lambda path: None)
    monkeypatch.setattr(hooks, "_run_index", lambda memory, cwd: None)

    _invoke(["recover", str(project)])

    assert record.exists()
    assert list(_memory(project).glob("*.md")) == []
