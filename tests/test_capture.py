"""Tests for memsearch.capture — the transcript parsers, the summarizer call and
the journal writer. Every test is hermetic: HOME, MEMSEARCH_CONFIG and TMPDIR
point into tmp_path, and the summarizers are fake shell scripts on PATH."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from memsearch import capture, config

NOW = datetime(2026, 7, 23, 12, 34, 56)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("MEMSEARCH_CONFIG", str(tmp_path / "config.toml"))
    for name in (
        "MEMSEARCH_DIR",
        "CLAUDE_PROJECT_DIR",
        "MEMSEARCH_DISABLE",
        "MEMSEARCH_IN_STOP_WORKER",
        "MEMSEARCH_PLUGIN_ROOT",
        "MEMSEARCH_SUMMARY_MAX_CHARS",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(capture, "_SAFE_MODE", None)
    monkeypatch.chdir(tmp_path)


def _jsonl(path: Path, rows: list) -> Path:
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _fake_bin(tmp_path, monkeypatch, name: str, body: str) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MEMSEARCH_TEST_LOG", str(tmp_path / "log"))
    (tmp_path / "log").mkdir(exist_ok=True)
    return script


CLAUDE_RECORDER = """
LOG="$MEMSEARCH_TEST_LOG"
if [ "${1:-}" = "--help" ]; then
  echo x >> "$LOG/help.calls"
  echo "Usage: claude [options]"
  __HELP_EXTRA__
  exit 0
fi
printf '%s\\0' "$@" > "$LOG/argv.bin"
cat > "$LOG/stdin.txt"
env > "$LOG/env.txt"
echo "- User asked about the hook."
echo "- Claude Code explained it."
"""


def _claude_recorder(tmp_path, monkeypatch, *, safe_mode: bool):
    body = CLAUDE_RECORDER.replace("__HELP_EXTRA__", 'echo "  --safe-mode  run safely"' if safe_mode else ":")
    return _fake_bin(tmp_path, monkeypatch, "claude", body)


def _log(tmp_path, name: str) -> str:
    return (tmp_path / "log" / name).read_text(encoding="utf-8")


def _argv(tmp_path) -> list[str]:
    raw = (tmp_path / "log" / "argv.bin").read_bytes().decode("utf-8")
    return raw.split("\0")[:-1]


# --- Claude transcripts ------------------------------------------------------


def _claude_transcript(path: Path) -> Path:
    return _jsonl(
        path,
        [
            {"type": "mode", "mode": "normal"},
            {"type": "file-history-snapshot", "snapshot": {}},
            {"type": "user", "isMeta": True, "uuid": "meta-1", "message": {"content": "<caveat>META_NOISE</caveat>"}},
            {"type": "user", "uuid": "turn-1", "message": {"content": "FIRST_TURN_QUESTION"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "FIRST_TURN_ANSWER"}]}},
            {"type": "progress", "data": {"status": "running"}},
            "{not valid json",
            {"type": "user", "uuid": "turn-2", "message": {"content": "LAST_TURN_QUESTION"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "SECRET_THINKING"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "TOOL_COMMAND"}},
                        {"type": "text", "text": "ASSISTANT_REPLY"},
                    ]
                },
            },
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "TOOL_RESULT_NOISE"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "FINAL_ANSWER"}]}},
            {"type": "system", "message": {"content": "system noise"}},
        ],
    )


def test_claude_extracts_only_the_last_turn(tmp_path):
    turn = capture.extract_last_turn(_claude_transcript(tmp_path / "session-a.jsonl"), "claude")

    assert turn.text == (
        "=== Transcript of a conversation between User and Claude Code ===\n"
        "[User]: LAST_TURN_QUESTION\n"
        "[Claude Code]: ASSISTANT_REPLY\n"
        "[Claude Code]: FINAL_ANSWER"
    )
    assert turn.usable
    assert turn.turn_uuid == "turn-2"
    assert turn.session_id == "session-a"
    assert turn.transcript_path == str(tmp_path / "session-a.jsonl")
    for noise in ("FIRST_TURN", "SECRET_THINKING", "TOOL_COMMAND", "TOOL_RESULT_NOISE", "META_NOISE", "system noise"):
        assert noise not in turn.text


def test_claude_summarizer_input_excludes_harness_blocks(tmp_path):
    """Upstream #227: reminders and task notices are not the user's words."""
    rows = [
        {
            "type": "user",
            "uuid": "u1",
            "message": {"content": "<system-reminder>English reminder</system-reminder>\nqual o status?"},
        },
        {"type": "assistant", "uuid": "a1", "message": {"content": [{"type": "text", "text": "Tudo verde."}]}},
    ]
    turn = capture.extract_last_turn(_jsonl(tmp_path / "s.jsonl", rows), "claude")
    assert "[User]: qual o status?" in turn.text
    assert "English reminder" not in turn.text and "system-reminder" not in turn.text


def test_claude_explicit_session_id_wins(tmp_path):
    turn = capture.extract_last_turn(_claude_transcript(tmp_path / "session-a.jsonl"), "claude", session_id="from-host")

    assert turn.session_id == "from-host"


def test_claude_array_content_user_message(tmp_path):
    path = _jsonl(
        tmp_path / "s.jsonl",
        [
            {"type": "user", "uuid": "u-9", "message": {"content": [{"type": "text", "text": "  ARRAY_QUESTION  "}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ARRAY_ANSWER"}]}},
        ],
    )

    turn = capture.extract_last_turn(path, "claude")

    assert "[User]: ARRAY_QUESTION" in turn.text
    assert turn.turn_uuid == "u-9"


def test_claude_survives_malformed_entries(tmp_path):
    path = _jsonl(
        tmp_path / "weird.jsonl",
        [
            {"type": "user", "message": "a string instead of an object"},
            {"type": "user", "message": None},
            {"type": "assistant", "message": {"content": "a string instead of blocks"}},
            {"type": "user", "uuid": "u-1", "message": {"content": "REAL_QUESTION"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "REAL_ANSWER"}]}},
        ],
    )

    turn = capture.extract_last_turn(path, "claude")

    assert turn.text.endswith("[User]: REAL_QUESTION\n[Claude Code]: REAL_ANSWER")
    assert turn.turn_uuid == "u-1"


def test_claude_empty_and_userless_transcripts_are_not_usable(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    userless = _jsonl(tmp_path / "userless.jsonl", [{"type": "assistant", "message": {"content": []}}])

    assert capture.extract_last_turn(empty, "claude").text == "(empty transcript)"
    assert not capture.extract_last_turn(empty, "claude").usable
    assert capture.extract_last_turn(userless, "claude").text == "(no user message found)"
    assert not capture.extract_last_turn(userless, "claude").usable
    assert not capture.extract_last_turn(tmp_path / "missing.jsonl", "claude").usable


# --- Codex rollouts ----------------------------------------------------------


def _codex_rollout(path: Path) -> Path:
    return _jsonl(
        path,
        [
            {"type": "session_meta", "payload": {"type": "session_meta"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "OLD_QUESTION"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "OLD_ANSWER"}},
            "{broken",
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "LAST_QUESTION\nsecond line"}},
            {"type": "response_item", "payload": {"type": "function_call", "arguments": "TOOL_COMMAND"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "TOOL_OUTPUT_NOISE"}},
            {"type": "event_msg", "payload": {"type": "agent_reasoning", "text": "REASONING_NOISE"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "LAST_ANSWER"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ],
    )


def test_codex_composes_final_exchange_and_context(tmp_path):
    turn = capture.extract_last_turn(
        _codex_rollout(tmp_path / "rollout-2026-07-23T10-00-00-abc.jsonl"),
        "codex",
        last_assistant_message="LAST_ANSWER",
    )

    assert turn.text == (
        "=== Final exchange, authoritative for outcome ===\n"
        "[User]: LAST_QUESTION\n"
        "[Codex final]: LAST_ANSWER\n"
        "\n"
        "=== Additional conversation context ===\n"
        "=== Transcript of a conversation between User and Codex CLI ===\n"
        "[User]: LAST_QUESTION\nsecond line\n"
        "[Codex]: LAST_ANSWER"
    )
    assert turn.session_id == "2026-07-23T10-00-00-abc"
    assert turn.user_question == "LAST_QUESTION"
    assert turn.last_message == "LAST_ANSWER"
    assert turn.usable
    for noise in ("OLD_QUESTION", "OLD_ANSWER", "TOOL_COMMAND", "TOOL_OUTPUT_NOISE", "REASONING_NOISE"):
        assert noise not in turn.text


def test_codex_without_last_message_uses_the_parsed_turn(tmp_path):
    turn = capture.extract_last_turn(_codex_rollout(tmp_path / "rollout-x.jsonl"), "codex")

    assert turn.text.startswith("=== Transcript of a conversation between User and Codex CLI ===")
    assert "Final exchange" not in turn.text


def test_codex_falls_back_to_the_last_user_message_without_task_started(tmp_path):
    path = _jsonl(
        tmp_path / "rollout-y.jsonl",
        [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "ONLY_QUESTION"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "ONLY_ANSWER"}},
        ],
    )

    turn = capture.extract_last_turn(path, "codex")

    assert "[User]: ONLY_QUESTION" in turn.text
    assert "[Codex]: ONLY_ANSWER" in turn.text


def test_codex_user_question_falls_back_to_history_jsonl(tmp_path, monkeypatch):
    codex_home = tmp_path / "codexhome"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _jsonl(
        codex_home / "history.jsonl",
        [
            {"session_id": "sess-1", "text": "EARLIER_PROMPT"},
            {"session_id": "other", "text": "OTHER_SESSION_PROMPT"},
            {"session_id": "sess-1", "text": "  HISTORY_PROMPT  "},
        ],
    )
    rollout = _jsonl(
        tmp_path / "rollout-sess-1.jsonl",
        [
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "ANSWER_ONLY"}},
        ],
    )

    turn = capture.extract_last_turn(rollout, "codex", last_assistant_message="ANSWER_ONLY")

    assert turn.session_id == "sess-1"
    assert turn.user_question == "HISTORY_PROMPT"
    assert "[User]: HISTORY_PROMPT" in turn.text
    assert "OTHER_SESSION_PROMPT" not in turn.text


def test_codex_without_transcript_uses_the_final_message_only(tmp_path):
    turn = capture.extract_last_turn("", "codex", session_id="s1", last_assistant_message="ONLY_FINAL")

    assert turn.text == "=== Final exchange, authoritative for outcome ===\n[Codex final]: ONLY_FINAL"
    assert turn.transcript_path == ""
    assert turn.usable


def test_codex_empty_input_is_not_usable(tmp_path):
    assert not capture.extract_last_turn("", "codex", session_id="s1").usable


def test_codex_truncates_the_last_message_and_the_content(tmp_path, monkeypatch):
    turn = capture.extract_last_turn("", "codex", session_id="s", last_assistant_message="y" * 5000)

    assert turn.last_message == "y" * 4000 + "...(truncated)"

    monkeypatch.setenv("MEMSEARCH_SUMMARY_MAX_CHARS", "120")
    capped = capture.extract_last_turn("", "codex", session_id="s", last_assistant_message="z" * 5000)

    assert capped.text.endswith("...(truncated)")
    assert len(capped.text) == 120 + len("...(truncated)")


def test_codex_user_question_is_first_line_capped_at_200(tmp_path):
    path = _jsonl(
        tmp_path / "rollout-q.jsonl",
        [{"type": "event_msg", "payload": {"type": "user_message", "message": "Q" * 260 + "\ntail"}}],
    )

    assert capture.extract_last_turn(path, "codex").user_question == "Q" * 200


# --- summarize ---------------------------------------------------------------


def test_summarize_claude_argv_stdin_and_env(tmp_path, monkeypatch):
    _claude_recorder(tmp_path, monkeypatch, safe_mode=True)

    summary, reason = capture.summarize(
        "TRANSCRIPT_BODY", platform="claude", model="haiku", prompt="PROMPT_BODY", cwd=tmp_path
    )

    assert reason == ""
    assert summary == "- User asked about the hook.\n- Claude Code explained it."
    assert _argv(tmp_path) == [
        "-p",
        "--safe-mode",
        "--strict-mcp-config",
        "--tools",
        "",
        "--model",
        "haiku",
        "--no-session-persistence",
        "--no-chrome",
    ]
    assert _log(tmp_path, "stdin.txt") == "PROMPT_BODY\n\nTranscript:\nTRANSCRIPT_BODY"
    env = dict(line.split("=", 1) for line in _log(tmp_path, "env.txt").splitlines() if "=" in line)
    assert env["MEMSEARCH_DISABLE"] == "1"
    assert env["CLAUDECODE"] == ""


def test_summarize_claude_omits_safe_mode_and_caches_the_probe(tmp_path, monkeypatch):
    _claude_recorder(tmp_path, monkeypatch, safe_mode=False)

    capture.summarize("A", platform="claude", model="haiku", prompt="P", cwd=tmp_path)
    capture.summarize("B", platform="claude", model="haiku", prompt="P", cwd=tmp_path)

    assert "--safe-mode" not in _argv(tmp_path)
    assert _log(tmp_path, "help.calls") == "x\n"  # probed once per process, then cached


def test_summarize_reports_a_non_zero_exit(tmp_path, monkeypatch):
    _fake_bin(tmp_path, monkeypatch, "claude", "echo boom >&2\nexit 7\n")

    assert capture.summarize("A", platform="claude", model="m", prompt="P") == ("", "summarizer exited with status 7")


def test_summarize_reports_empty_output(tmp_path, monkeypatch):
    _fake_bin(tmp_path, monkeypatch, "claude", "printf '   \\n'\n")

    assert capture.summarize("A", platform="claude", model="m", prompt="P") == ("", "summarizer returned empty output")


def test_summarize_rejects_prose_without_bullets(tmp_path, monkeypatch):
    """Upstream #527: a rate-limit notice with exit 0 must not become the turn's summary."""
    _fake_bin(
        tmp_path, monkeypatch, "claude", 'echo "You\'ve hit your limit \xc2\xb7 resets 11am (Africa/Johannesburg)"\n'
    )

    assert capture.summarize("A", platform="claude", model="m", prompt="P") == (
        "",
        "summarizer returned no bullet points",
    )


def test_summarize_accepts_any_bullet_marker(tmp_path, monkeypatch):
    _fake_bin(tmp_path, monkeypatch, "claude", "printf 'Notes:\\n* User asked X\\n'\n")

    assert capture.summarize("A", platform="claude", model="m", prompt="P") == ("Notes:\n* User asked X", "")


def test_summarize_reports_a_timeout(tmp_path, monkeypatch):
    _fake_bin(tmp_path, monkeypatch, "claude", 'if [ "${1:-}" = "--help" ]; then exit 0; fi\nexec sleep 30\n')

    assert capture.summarize("A", platform="claude", model="m", prompt="P", timeout=0.3) == (
        "",
        "summarizer timed out",
    )


def test_summarize_reports_a_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))

    assert capture.summarize("A", platform="claude", model="m", prompt="P") == ("", "summarizer unavailable")


def test_summarize_replaces_invalid_utf8(tmp_path, monkeypatch):
    _fake_bin(tmp_path, monkeypatch, "claude", "printf -- '- caf\\xe9 broken\\n'\n")

    summary, reason = capture.summarize("A", platform="claude", model="m", prompt="P")

    assert reason == ""
    assert summary == "- caf\ufffd broken"


def test_summarize_codex_argv_and_env(tmp_path, monkeypatch):
    _fake_bin(
        tmp_path,
        monkeypatch,
        "codex",
        'LOG="$MEMSEARCH_TEST_LOG"\nprintf \'%s\\0\' "$@" > "$LOG/argv.bin"\nenv > "$LOG/env.txt"\n'
        'echo "- Codex noted the outcome."\n',
    )

    summary, reason = capture.summarize(
        "TRANSCRIPT_BODY", platform="codex", model="gpt-5.1-codex-mini", prompt="PROMPT_BODY", cwd=tmp_path
    )

    assert (summary, reason) == ("- Codex noted the outcome.", "")
    assert _argv(tmp_path) == [
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "-c",
        "features.hooks=false",
        "-c",
        'model_reasoning_effort="low"',
        "-m",
        "gpt-5.1-codex-mini",
        "PROMPT_BODY\n\nHere is the transcript:\n\nTRANSCRIPT_BODY",
    ]
    env = dict(line.split("=", 1) for line in _log(tmp_path, "env.txt").splitlines() if "=" in line)
    assert env["MEMSEARCH_DISABLE"] == "1"
    assert env["MEMSEARCH_IN_STOP_WORKER"] == "1"


# --- fallbacks and prompts ---------------------------------------------------


def test_mechanical_summary_branches():
    assert capture.mechanical_summary("Q", "A", "CONTENT") == "- User asked: Q\n- Codex: A"
    assert capture.mechanical_summary("", "A", "CONTENT") == "- Codex: A"
    assert capture.mechanical_summary("", "", "CONTENT") == "CONTENT"
    assert capture.mechanical_summary("Q", "b" * 900, "C") == f"- User asked: Q\n- Codex: {'b' * 800}..."
    assert capture.mechanical_summary("Q", "b" * 800, "C") == f"- User asked: Q\n- Codex: {'b' * 800}"


def test_unavailable_line_text():
    assert capture.unavailable_line("summarizer timed out") == (
        "- Memory summary unavailable: summarizer timed out; transcript content was omitted. "
        "Use the transcript anchor for progressive disclosure."
    )


def test_load_prompt_precedence(tmp_path, monkeypatch):
    plugin_root = tmp_path / "plugin"
    (plugin_root / "prompts").mkdir(parents=True)
    (plugin_root / "prompts" / "summarize.txt").write_text("PLUGIN {{AGENT_NAME}} template\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / "custom.txt").write_text("CUSTOM {{AGENT_NAME}} template\n", encoding="utf-8")
    cfg = config.Config()

    assert capture.load_prompt(cfg, "claude", project) == capture.FALLBACK_PROMPT

    monkeypatch.setenv("MEMSEARCH_PLUGIN_ROOT", str(plugin_root))
    assert capture.load_prompt(cfg, "claude", project) == "PLUGIN Claude Code template"
    assert capture.load_prompt(cfg, "codex", project) == "PLUGIN Codex template"

    cfg.prompts.summarize = "custom.txt"  # relative to the project directory
    assert capture.load_prompt(cfg, "codex", project) == "CUSTOM Codex template"

    cfg.prompts.summarize = str(project / "custom.txt")  # absolute
    assert capture.load_prompt(cfg, "claude", project) == "CUSTOM Claude Code template"

    cfg.prompts.summarize = str(tmp_path / "missing.txt")  # unreadable: next source wins
    assert capture.load_prompt(cfg, "claude", project) == "PLUGIN Claude Code template"


def test_load_prompt_uses_the_repository_template(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMSEARCH_PLUGIN_ROOT", str(Path(__file__).resolve().parent.parent))

    prompt = capture.load_prompt(config.Config(), "codex", tmp_path)

    assert prompt.startswith("You are a third-person note-taker.")
    assert "{{AGENT_NAME}}" not in prompt
    assert "Codex" in prompt


# --- journal -----------------------------------------------------------------


def _append(memory, **kwargs):
    defaults = {
        "session_id": "session-a",
        "turn_uuid": "turn-a",
        "transcript_path": "/tmp/session-a.jsonl",
        "summary": "- Captured a session summary.",
        "anchor_kind": "transcript",
        "now": NOW,
    }
    return capture.append_to_journal(memory, **{**defaults, **kwargs})


def test_journal_claude_golden_and_lazy_session_heading(tmp_path):
    memory = tmp_path / "memory"

    path = _append(memory)
    first = (
        "\n## Session 12:34\n"
        "\n"
        "### 12:34\n"
        "<!-- session:session-a turn:turn-a transcript:/tmp/session-a.jsonl -->\n"
        "- Captured a session summary.\n"
        "\n"
    )
    assert path == memory / "2026-07-23.md"
    assert path.read_text(encoding="utf-8") == first

    _append(memory, summary="- A second turn of the same session.")
    second = first + (
        "### 12:34\n"
        "<!-- session:session-a turn:turn-a transcript:/tmp/session-a.jsonl -->\n"
        "- A second turn of the same session.\n"
        "\n"
    )
    assert path.read_text(encoding="utf-8") == second

    _append(
        memory,
        session_id="session-b",
        turn_uuid="turn-b",
        transcript_path="/tmp/session-b.jsonl",
        summary="- A different session.",
    )
    assert path.read_text(encoding="utf-8") == second + (
        "\n## Session 12:34\n"
        "\n"
        "### 12:34\n"
        "<!-- session:session-b turn:turn-b transcript:/tmp/session-b.jsonl -->\n"
        "- A different session.\n"
        "\n"
    )


def test_journal_codex_rollout_anchor_golden(tmp_path):
    memory = tmp_path / "memory"
    codex = {"session_id": "sess-9", "turn_uuid": "", "transcript_path": "/tmp/rollout.jsonl", "anchor_kind": "rollout"}

    path = _append(memory, summary="- User asked: Q\n- Codex: A", **codex)
    first = (
        "\n## Session 12:34\n"
        "\n"
        "### 12:34\n"
        "<!-- session:sess-9 rollout:/tmp/rollout.jsonl -->\n"
        "- User asked: Q\n"
        "- Codex: A\n"
        "\n"
    )
    assert path.read_text(encoding="utf-8") == first

    _append(memory, summary="- A second Codex turn.", **codex)
    second = first + "### 12:34\n<!-- session:sess-9 rollout:/tmp/rollout.jsonl -->\n- A second Codex turn.\n\n"
    assert path.read_text(encoding="utf-8") == second

    _append(memory, summary="- Another Codex session.", **{**codex, "session_id": "sess-10"})
    assert path.read_text(encoding="utf-8") == second + (
        "\n## Session 12:34\n"
        "\n"
        "### 12:34\n"
        "<!-- session:sess-10 rollout:/tmp/rollout.jsonl -->\n"
        "- Another Codex session.\n"
        "\n"
    )


def test_journal_without_session_id_writes_no_anchor(tmp_path):
    memory = tmp_path / "memory"

    _append(memory, session_id="")
    _append(memory, session_id="")

    assert (memory / "2026-07-23.md").read_text(encoding="utf-8") == 2 * (
        "\n## Session 12:34\n\n### 12:34\n- Captured a session summary.\n\n"
    )


def test_journal_creates_the_memory_directory(tmp_path):
    memory = tmp_path / "deep" / "memory"

    _append(memory)

    assert memory.is_dir()
