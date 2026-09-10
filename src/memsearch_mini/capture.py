"""Capture one conversation turn, summarize it, append it to the journal.

Faithful port of the upstream bash hooks (``parse-transcript.sh``,
``parse-rollout.sh``, both ``stop.sh``): the on-disk journal format and the
summarizer command lines are byte-for-byte identical to the upstream ones.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .transcript import strip_harness_tags

# What the upstream parsers print instead of a transcript. Never summarized.
SENTINELS = frozenset({"(empty transcript)", "(empty rollout)", "(no user message found)", "(empty turn)"})
MAX_CONTENT_CHARS = 8000  # env override: MEMSEARCH_MINI_SUMMARY_MAX_CHARS
LAST_MESSAGE_CHARS = 4000
MECHANICAL_CHARS = 800
USER_QUESTION_CHARS = 200
AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}
_HEADER = "=== Transcript of a conversation between User and {} ==="
_FINAL_HEADER = "=== Final exchange, authoritative for outcome ==="
_EXTRA_HEADER = "=== Additional conversation context ==="
_BULLET_LINE = re.compile(r"^\s*[-*\u2022]\s", re.MULTILINE)
FALLBACK_PROMPT = (
    "You are a third-person note-taker. Summarize the transcript as 2-10 bullet points. "
    "Write in third person. Mandatory language rule: write every bullet in the same primary "
    "language as the [User] text. If User mixes languages, use the dominant user-facing language. "
    "Do NOT answer User's question. Output ONLY bullet points."
)


@dataclass
class ParsedTurn:
    """The last turn of a conversation, ready to be summarized."""

    text: str = ""
    session_id: str = ""
    turn_uuid: str = ""
    transcript_path: str = ""
    user_question: str = ""
    last_message: str = ""

    @property
    def usable(self) -> bool:
        stripped = self.text.strip()
        return bool(stripped) and stripped not in SENTINELS


def _loads(raw: str) -> dict | None:
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _read_lines(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.readlines()
    except OSError:
        return []


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _truncate(text: str, limit: int, marker: str) -> str:
    return f"{text[:limit]}{marker}" if len(text) > limit else text


def _claude_texts(obj: dict, *, plain: bool) -> list[str]:
    """Non-empty text of a Claude entry; tool_use/tool_result/thinking are skipped."""
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    clean = strip_harness_tags if plain else str.strip  # user entries carry harness-injected blocks
    if isinstance(content, str):
        return [clean(content)] if plain and clean(content) else []
    if not isinstance(content, list):
        return []
    blocks = [b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return [text for text in (clean(block) for block in blocks) if text]


def _is_user_turn(obj: dict | None) -> bool:
    return bool(obj and obj.get("type") == "user" and not obj.get("isMeta") and _claude_texts(obj, plain=True))


def _claude_turn(lines: list[str], session_id: str, path: Path | None, turn_uuid: str = "") -> ParsedTurn:
    """The last turn, or — when ``turn_uuid`` is given — the turn that user entry starts.

    A recovered turn may no longer be the last one (the session was resumed), so it is cut at
    the next user message instead of at the end of the file.
    """
    turn = ParsedTurn(session_id=session_id, transcript_path=str(path) if path is not None else "")
    if not lines:
        turn.text = "(empty transcript)"
        return turn
    start = end = None
    if turn_uuid:
        for index, raw in enumerate(lines):
            obj = _loads(raw)
            if start is None:
                if _is_user_turn(obj) and str(obj.get("uuid") or "") == turn_uuid:
                    start = index
            elif _is_user_turn(obj):
                end = index
                break
    else:
        for index in range(len(lines) - 1, -1, -1):
            if _is_user_turn(_loads(lines[index])):
                start = index
                break
    if start is None:
        turn.text = "(no user message found)"
        return turn
    turn.turn_uuid = str((_loads(lines[start]) or {}).get("uuid") or "")
    out = [_HEADER.format("Claude Code")]
    for raw in lines[start:end]:
        obj = _loads(raw)
        if obj is None or obj.get("type") not in ("user", "assistant"):
            continue
        is_user = obj.get("type") == "user"
        label = "User" if is_user else "Claude Code"
        out.extend(f"[{label}]: {text}" for text in _claude_texts(obj, plain=is_user))
    formatted = "\n".join(out)
    turn.text = formatted if formatted.strip() else "(empty turn)"
    return turn


def _codex_event(obj: dict, kind: str) -> str:
    if obj.get("type") != "event_msg":
        return ""
    payload = obj.get("payload") or {}
    if not isinstance(payload, dict) or payload.get("type") != kind:
        return ""
    message = payload.get("message")
    return message.strip() if isinstance(message, str) else ""


def _parse_codex(lines: list[str]) -> tuple[str, str]:
    """Return (formatted last turn or sentinel, last user question of the rollout)."""
    question = ""
    for raw in lines:
        obj = _loads(raw)
        question = (obj and _codex_event(obj, "user_message")) or question
    question = question.split("\n")[0][:USER_QUESTION_CHARS] if question else ""
    if not lines:
        return "(empty rollout)", question
    start = fallback = None  # last task_started; last user_message as fallback
    for index in range(len(lines) - 1, -1, -1):
        obj = _loads(lines[index]) or {}
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "event_msg" and payload.get("type") == "task_started":
            start = index
            break
        if fallback is None and _codex_event(obj, "user_message"):
            fallback = index
    start = start if start is not None else fallback
    if start is None:
        return "(no user message found)", question
    out = [_HEADER.format("Codex CLI")]
    for raw in lines[start:]:
        obj = _loads(raw)
        if obj is None:
            continue
        user, agent = _codex_event(obj, "user_message"), _codex_event(obj, "agent_message")
        if user:
            out.append(f"[User]: {user}")
        elif agent:
            out.append(f"[Codex]: {agent}")
    formatted = "\n".join(out)
    return (formatted if formatted.strip() else "(empty turn)"), question


def _history_question(session_id: str) -> str:
    """Fallback for rollouts with no user_message event: Codex's own history.jsonl."""
    if not session_id:
        return ""
    history = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "history.jsonl"
    latest = ""
    for raw in _read_lines(history):
        obj = _loads(raw)
        if obj and obj.get("session_id") == session_id and isinstance(obj.get("text"), str):
            latest = obj["text"].strip() or latest
    return latest


def _codex_turn(path: Path | None, session_id: str, last_message: str) -> ParsedTurn:
    lines = _read_lines(path)
    parsed, question = _parse_codex(lines) if lines else ("", "")
    question = question or _history_question(session_id)
    extra = f"\n\n{_EXTRA_HEADER}\n{parsed}" if parsed and parsed not in SENTINELS else ""
    if last_message and question:
        content = f"{_FINAL_HEADER}\n[User]: {question}\n[Codex final]: {last_message}{extra}"
    elif last_message:
        content = f"{_FINAL_HEADER}\n[Codex final]: {last_message}{extra}"
    elif extra:
        content = parsed
    elif question:
        content = f"[User]: {question}"
    else:
        content = ""
    raw_limit = os.environ.get("MEMSEARCH_MINI_SUMMARY_MAX_CHARS") or ""
    limit = int(raw_limit) if raw_limit.isdigit() else MAX_CONTENT_CHARS
    return ParsedTurn(
        text=_truncate(content, limit, "...(truncated)"),
        session_id=session_id,
        transcript_path=str(path) if path is not None else "",
        user_question=question,
        last_message=last_message,
    )


def extract_last_turn(
    transcript_path: str | os.PathLike[str],
    platform: str,
    *,
    session_id: str = "",
    last_assistant_message: str = "",
    turn_uuid: str = "",
) -> ParsedTurn:
    """Parse the last turn of a Claude Code transcript or of a Codex rollout.

    ``turn_uuid`` (Claude only) selects one specific turn instead of the last: the recovery
    path uses it for a turn whose Stop hook died before the journal was written.
    """
    path = Path(transcript_path) if transcript_path else None
    stem = path.stem if path is not None else ""
    if platform == "codex":
        default_id = stem[len("rollout-") :] if stem.startswith("rollout-") else stem
        last_message = _truncate(last_assistant_message, LAST_MESSAGE_CHARS, "...(truncated)")
        return _codex_turn(path, session_id or default_id, last_message)
    return _claude_turn(_read_lines(path), session_id or stem, path, turn_uuid)


_SAFE_MODE: bool | None = None


def _claude_safe_mode() -> bool:
    """``claude --help`` is probed once per process: the flag is version-dependent."""
    global _SAFE_MODE
    if _SAFE_MODE is None:
        try:
            probe = subprocess.run(["claude", "--help"], capture_output=True, timeout=10)
            _SAFE_MODE = b"--safe-mode" in probe.stdout
        except Exception:
            _SAFE_MODE = False
    return _SAFE_MODE


def _summarizer_command(platform: str, model: str, prompt: str, text: str) -> tuple[list[str], bytes, dict[str, str]]:
    if platform == "codex":
        # The inner double quotes of the -c value are literal: it is a TOML value.
        argv = ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only"]
        argv += ["-c", "features.hooks=false", "-c", 'model_reasoning_effort="low"', "-m", model]
        argv.append(f"{prompt}\n\nHere is the transcript:\n\n{text}")
        return argv, b"", {"MEMSEARCH_MINI_DISABLE": "1", "MEMSEARCH_MINI_IN_STOP_WORKER": "1"}
    argv = ["claude", "-p"]
    if _claude_safe_mode():
        argv.append("--safe-mode")
    argv += ["--strict-mcp-config", "--tools", "", "--model", model, "--no-session-persistence", "--no-chrome"]
    # The prompt travels on stdin so transcript size never reaches argv limits.
    return argv, f"{prompt}\n\nTranscript:\n{text}".encode(), {"MEMSEARCH_MINI_DISABLE": "1", "CLAUDECODE": ""}


def summarize(
    text: str,
    *,
    platform: str,
    model: str,
    prompt: str,
    cwd: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
) -> tuple[str, str]:
    """Return ``(summary, failure_reason)``; exactly one is non-empty. Never raises."""
    argv, payload, extra_env = _summarizer_command(platform, model, prompt, text)
    if timeout is None:
        timeout = 30 if platform == "codex" else 110
    try:
        proc = subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            env={**os.environ, **extra_env},
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "", "summarizer timed out"
    except Exception:
        return "", "summarizer unavailable"
    if proc.returncode != 0:
        return "", f"summarizer exited with status {proc.returncode}"
    summary = proc.stdout.decode("utf-8", errors="replace").strip()
    if not summary:
        return "", "summarizer returned empty output"
    # The prompt contracts bullet points only. A rate-limit notice or a refusal can come back
    # with exit 0 as plain prose; that must never be written down as the turn's summary.
    if not _BULLET_LINE.search(summary):
        return "", "summarizer returned no bullet points"
    return summary, ""


def mechanical_summary(user_question: str, last_message: str, content: str) -> str:
    """Summary of last resort: the final exchange, verbatim and truncated."""
    if not last_message:
        return content
    truncated = _truncate(last_message, MECHANICAL_CHARS, "...")
    if user_question:
        return f"- User asked: {user_question}\n- Codex: {truncated}"
    return f"- Codex: {truncated}"


def unavailable_line(reason: str) -> str:
    return (
        f"- Memory summary unavailable: {reason}; transcript content was omitted. "
        "Use the transcript anchor for progressive disclosure."
    )


def load_prompt(cfg, platform: str, project_dir: str | os.PathLike[str] | None = None) -> str:
    """Custom template (config) > ``$MEMSEARCH_MINI_PLUGIN_ROOT/prompts/summarize.txt`` > built-in."""
    text = ""
    custom = (getattr(cfg.prompts, "summarize", "") or "").strip()
    if custom:
        path = Path(custom).expanduser()
        if not path.is_absolute() and project_dir:
            path = Path(project_dir) / path
        text = _read_text(path)
    root = os.environ.get("MEMSEARCH_MINI_PLUGIN_ROOT", "")
    if not text and root:
        text = _read_text(Path(root) / "prompts" / "summarize.txt")
    return (text or FALLBACK_PROMPT).rstrip().replace("{{AGENT_NAME}}", AGENT_NAMES.get(platform, platform))


def journal_suffix(cfg) -> str:
    """Per-writer filename suffix: "" (default) or "-<short hostname>" when configured.

    Two machines appending to one synced memory folder would otherwise overwrite each other's
    daily file; a suffix gives each writer its own journal while search still sees them all.
    """
    if getattr(cfg.memory, "filename_suffix", "") != "hostname":
        return ""
    host = re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname().split(".")[0]).strip("-")
    return f"-{host}" if host else ""


def append_to_journal(
    memory_dir: str | os.PathLike[str],
    *,
    session_id: str,
    turn_uuid: str,
    transcript_path: str,
    summary: str,
    anchor_kind: str = "transcript",
    now: datetime | None = None,
    suffix: str = "",
) -> Path:
    """Append one entry to ``<memory_dir>/YYYY-MM-DD<suffix>.md``, with a single write."""
    stamp = now or datetime.now()
    directory = Path(memory_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp:%Y-%m-%d}{suffix}.md"
    clock = f"{stamp:%H:%M}"
    # The anchor comment doubles as the "session heading already written" marker.
    lazy = not session_id or f"session:{session_id}" not in _read_text(path)
    block = f"\n## Session {clock}\n\n" if lazy else ""
    block += f"### {clock}\n"
    if session_id:
        anchor = (
            f"session:{session_id} rollout:{transcript_path}"
            if anchor_kind == "rollout"
            else f"session:{session_id} turn:{turn_uuid} transcript:{transcript_path}"
        )
        block += f"<!-- {anchor} -->\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{block}{summary}\n\n")
    return path
