"""The ``memsearch-mini hook`` commands: everything Claude Code and Codex invoke.

Deliberately dependency-light: the index is inspected with the stdlib
``sqlite3`` only, so no hook ever imports the store, numpy or an embedding
backend. Every command prints one JSON object and exits 0, whatever happens.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from functools import wraps
from importlib.metadata import version as _dist_version
from pathlib import Path

import click

from . import capture, config

RECENT_MEMORY_MAX_LINES = 40
# Claude Code head-truncates inline hook context at roughly 2 KB; stay under it.
RECENT_MEMORY_MAX_BYTES = 1800
_DAILY_JOURNAL = re.compile(r"^\d{4}-\d{2}-\d{2}(-[A-Za-z0-9_-]+)?\.md$")  # optional per-writer suffix
_H2 = re.compile(r"^##\s")
_H34 = re.compile(r"^#{3,4}\s")
_BULLET = re.compile(r"^-\s")
_KEY_TIP = "Tip: memsearch-mini config set embedding.provider onnx"
_ONNX_TIP = "Tip: uv sync --extra onnx"
_SUMMARIZE_TIP = "Tip: memsearch-mini config set summarize.mode harness"


def _as_dict(data: bytes | str) -> dict | None:
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    try:
        obj = json.loads(data)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def read_payload(stream=None, timeout: float = 2.0) -> dict:
    """The hook payload from stdin: returns as soon as the buffer parses as JSON.

    Hosts keep the pipe open after writing, so waiting for EOF would stall the
    session. A closed, silent or malformed stdin yields ``{}``.
    """
    stream = sys.stdin if stream is None else stream
    try:
        fd = stream.fileno()
    except Exception:
        fd = None
    if fd is None:  # no real file descriptor (CliRunner, StringIO)
        try:
            return _as_dict(stream.read()) or {}
        except Exception:
            return {}
    buffer = b""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
            chunk = os.read(fd, 65536) if ready else b""
        except Exception:
            break
        if not chunk:  # EOF or nothing readable before the deadline
            break
        buffer += chunk
        parsed = _as_dict(buffer)
        if parsed is not None:
            return parsed
    return _as_dict(buffer) or {}


def resolve_project_dir(payload: dict) -> Path:
    """``CLAUDE_PROJECT_DIR`` > payload cwd > process cwd, then the git root."""
    override = os.environ.get("CLAUDE_PROJECT_DIR") or ""
    payload_cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
    if override and Path(override).is_dir():
        directory = Path(override)
    else:
        directory = Path(payload_cwd) if payload_cwd else Path.cwd()
    directory = Path(os.path.abspath(directory))
    top = _git(directory, "rev-parse", "--show-toplevel")
    if not top:
        return directory
    directory = Path(top)
    # A linked worktree shares the primary checkout's memory: its git-dir differs from the
    # common dir (<primary>/.git). In a submodule both are the same path, so it keeps its own.
    dirs = (_git(directory, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir") or "").splitlines()
    if len(dirs) == 2 and dirs[0] != dirs[1]:
        directory = Path(dirs[1]).parent
    return directory


def _git(cwd: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=2)
    except Exception:
        return ""
    return proc.stdout.decode("utf-8", errors="replace").strip() if proc.returncode == 0 else ""


def memsearch_mini_dir(project_dir: str | os.PathLike[str]) -> Path:
    override = os.environ.get("MEMSEARCH_MINI_DIR")
    return Path(override).expanduser() if override else Path(project_dir) / ".memsearch-mini"


def memory_dir(project_dir: str | os.PathLike[str]) -> Path:
    return memsearch_mini_dir(project_dir) / "memory"


def _index_argv(memory: str | os.PathLike[str]) -> list[str]:
    return [sys.executable, "-m", "memsearch_mini", "index", str(memory), "--skip-if-locked"]


def _reindex_env() -> dict[str, str]:
    return {**os.environ, "MEMSEARCH_MINI_DISABLE": "1"}


def _index_log():
    """Where background indexers write: ``$MEMSEARCH_MINI_HOME/index.log`` (a plain file,
    never the hook's own stdout/stderr pipe). Truncated once it passes 1 MB."""
    try:
        path = config.home_dir() / "index.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_048_576:
            path.write_text("")
        return open(path, "a")  # handed to a subprocess; closed when it exits
    except OSError:
        return subprocess.DEVNULL


def _spawn_detached(argv: list[str], cwd: str | os.PathLike[str], env: dict[str, str]) -> None:
    """Fire and forget: own session, stdin closed, output to the index log, never waited on."""
    try:
        log = _index_log()
        subprocess.Popen(argv, start_new_session=True, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log)  # fmt: skip
    except Exception:
        traceback.print_exc(file=sys.stderr)


def _run_index(memory: str | os.PathLike[str], cwd: str | os.PathLike[str]) -> None:
    """Inline reindex — only for the already detached Codex stop worker."""
    try:
        log = _index_log()
        subprocess.run(_index_argv(memory), cwd=str(cwd), env=_reindex_env(), stdin=subprocess.DEVNULL,
                       stdout=log, stderr=log)  # fmt: skip
    except Exception:
        traceback.print_exc(file=sys.stderr)


# --- pending turns: recovery of a Stop hook that died mid-summary -------------
#
# The Claude Stop hook is async and can take up to 110 s (``claude -p``). Quit the host in
# that window — or run it under a wrapper that kills the whole process tree on exit — and the
# turn is lost without a trace. So the hook records *where* the turn is (transcript path,
# turn uuid, timestamp; never its content) in ``<project>/.memsearch-mini/pending/`` before it
# starts summarizing, and deletes the record once the journal is written. A record still
# there after the hook's 120 s timeout belongs to a dead hook: the next SessionStart or Stop
# in that project hands it to a detached ``hook recover``, which re-reads the turn from the
# transcript and writes it into the journal of the day it happened.

PENDING_GRACE_SECONDS = 150  # a Stop hook cannot outlive its 120 s timeout
PENDING_WORKING_STALE_SECONDS = 1800  # a recover worker that died mid-turn
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")


def pending_dir(project_dir: str | os.PathLike[str]) -> Path:
    return memsearch_mini_dir(project_dir) / "pending"


def _pending_write(project_dir: Path, turn, now: datetime) -> Path | None:
    """Record a turn about to be summarized; ``None`` when the record could not be written."""
    if not turn.transcript_path or not turn.turn_uuid:
        return None
    directory = pending_dir(project_dir)
    name = f"{_UNSAFE_NAME.sub('_', turn.session_id) or 'session'}-{_UNSAFE_NAME.sub('_', turn.turn_uuid)}.json"
    record = {
        "platform": "claude",
        "now": now.isoformat(),
        "session_id": turn.session_id,
        "turn_uuid": turn.turn_uuid,
        "transcript_path": turn.transcript_path,
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(json.dumps(record), encoding="utf-8")
        return path
    except OSError:
        traceback.print_exc(file=sys.stderr)
        return None


def _pending_discard(path: Path | None) -> None:
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink()


def _stale_pending(directory: Path, now: float | None = None) -> list[Path]:
    """Records whose hook is certainly dead, plus claims left behind by a dead worker."""
    clock = time.time() if now is None else now
    stale: list[Path] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return stale
    for path in entries:
        if path.name.endswith(".json"):
            limit = PENDING_GRACE_SECONDS
        elif path.name.endswith(".working"):
            limit = PENDING_WORKING_STALE_SECONDS
        else:
            continue
        try:
            if clock - path.stat().st_mtime >= limit:
                stale.append(path)
        except OSError:
            continue
    return stale


def _sweep_pending(project_dir: Path) -> str:
    """Spawn one detached recovery worker when a dead hook left turns behind.

    Returns a status fragment for the host: what is being recovered now, or what is still
    too young to touch (its hook may be running; the next turn end will pick it up).
    """
    directory = pending_dir(project_dir)
    stale = _stale_pending(directory)
    if stale:
        _spawn_detached(
            [sys.executable, "-m", "memsearch_mini", "hook", "recover", str(project_dir)],
            project_dir,
            {**os.environ, "MEMSEARCH_MINI_IN_STOP_WORKER": "1"},
        )
        return f"recovering {len(stale)} earlier turn(s) in the background"
    try:
        young = sum(1 for path in directory.iterdir() if path.name.endswith((".json", ".working")))
    except OSError:
        young = 0
    return f"{young} turn(s) pending, recovery at the next turn end" if young else ""


def _pending_claim(path: Path) -> Path | None:
    """Rename the record to ``<name>.<pid>.working``; atomic, so two workers never share one."""
    base = path.name.split(".json", 1)[0] + ".json"
    target = path.with_name(f"{base}.{os.getpid()}.working")
    try:
        path.rename(target)
    except OSError:
        return None
    # rename() keeps the record's mtime: without this the claim of an old record is stale the
    # instant it is made, and the next recovery worker journals the same turn a second time.
    with contextlib.suppress(OSError):
        os.utime(target)
    return target


def _already_journaled(memory: Path, turn_uuid: str, stamp: datetime, suffix: str) -> bool:
    """The append may have landed before the worker died: never write a turn twice."""
    journal = memory / f"{stamp:%Y-%m-%d}{suffix}.md"
    try:
        return f"turn:{turn_uuid}" in journal.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


# --- orphaned runtime environments --------------------------------------------
#
# Environments are keyed by the plugin path (``_venv_id`` in hooks/common.sh) and Claude Code
# installs the plugin under a versioned directory, so every update builds a fresh ~90 MB
# environment and abandons the previous one. ``sync_now`` records the owning checkout in
# ``<venv>.root``; once that path is gone — Claude Code deletes the old version directory about
# two weeks later — nothing can reach the environment again, so a SessionStart reclaims it.
# Stdlib only, like the rest of this module: no store, no numpy, no ONNX.

VENV_LOCK_STALE_SECONDS = 1800  # same grace as the stale sync lock in hooks/common.sh


def _sweep_orphan_venvs() -> None:
    """Delete runtime environments whose checkout no longer exists. Best effort, never raises."""
    venvs = config.home_dir() / "venvs"
    if not venvs.is_dir():
        return
    try:
        sidecars = sorted(venvs.glob("*.root"))
    except OSError:
        return
    for sidecar in sidecars:
        stem = sidecar.name[: -len(".root")]
        if not stem:
            continue  # glob matches a bare ".root", and `venvs / ""` is venvs itself
        try:
            recorded = sidecar.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        owner = recorded[0].strip() if recorded else ""
        try:
            if not owner or Path(owner).is_dir():
                continue  # the checkout is still installed: the environment is in use
        except (OSError, ValueError):
            continue
        lock = venvs / f"{stem}.lock"
        try:
            if time.time() - lock.stat().st_mtime < VENV_LOCK_STALE_SECONDS:
                continue  # a sync may be in flight; only a stale lock means nobody is there
        except OSError:
            pass  # no lock at all
        shutil.rmtree(venvs / stem, ignore_errors=True)
        shutil.rmtree(lock, ignore_errors=True)  # the lock is a directory: `mkdir` is the lock
        for path in (sidecar, venvs / f"{stem}.log"):
            with contextlib.suppress(OSError):
                path.unlink()


def _index_state(db_path: Path, memory: Path) -> tuple[str, bool]:
    """(status fragment, reindex needed), read from the derived index with sqlite3."""
    count: int | None = None
    last: float | None = None
    db_path = Path(os.path.abspath(db_path))
    if db_path.is_file():
        with contextlib.suppress(sqlite3.Error):
            conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=1)
            with contextlib.suppress(sqlite3.Error, TypeError, ValueError):
                count = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            with contextlib.suppress(sqlite3.Error, TypeError, ValueError):
                row = conn.execute("SELECT value FROM meta WHERE key = 'last_index_at'").fetchone()
                last = float(row[0]) if row else None
            conn.close()
    if count is None or last is None:
        return "not built yet — building in background", True
    stale = False
    with contextlib.suppress(OSError):
        stale = any(path.stat().st_mtime > last for path in memory.glob("*.md"))
    label = f"{count} chunks, updated {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}"
    return (f"{label} — stale — reindexing in background" if stale else label), stale


def _provider_problem(cfg) -> str:
    """Reason the configured provider cannot embed anything, as a status suffix."""
    missing = config.missing_api_key(cfg)
    if missing:
        return f" | ERROR: {missing} not set — memory search disabled | {_KEY_TIP}"
    try:
        installed = cfg.embedding.provider != "onnx" or importlib.util.find_spec("onnxruntime") is not None
    except Exception:
        installed = False
    if not installed:
        return f" | ERROR: onnxruntime not installed — memory search disabled | {_ONNX_TIP}"
    return ""


def _summarize_warning(cfg) -> str:
    """Status suffix when ``[summarize] mode = "api"`` is set but cannot run.

    A warning, not an error: search and indexing are unaffected, and every turn is still
    journaled — only without its bullets. Reported once per session, where it is read.
    """
    problem = config.summarize_problem(cfg)
    if not problem:
        return ""
    return f" | WARNING: {problem} — turns recorded without summaries | {_SUMMARIZE_TIP}"


def _preview_sections(path: Path, max_lines: int) -> list[str]:
    """Session sections that carry at least one bullet, newest ``max_lines`` kept."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    section: list[str] = []
    has_body = False
    for line in text.splitlines():
        if _H2.match(line):
            if has_body:
                out.extend(section)
            section, has_body = [line], False
        elif _H34.match(line):
            section.append(line)
        elif _BULLET.match(line):
            section.append(line)
            has_body = True
    if has_body:
        out.extend(section)
    return out[-max_lines:]


def _tail_within_bytes(lines: list[str], budget: int) -> list[str]:
    """The newest whole lines that fit in ``budget`` bytes (never a partial line)."""
    total, start = 0, len(lines)
    for index in range(len(lines) - 1, -1, -1):
        size = len(lines[index].encode("utf-8")) + 1
        if total + size > budget:
            break
        total += size
        start = index
    return lines[start:]


def recent_memory(memory: Path) -> str:
    """Cold-start context: the two newest journals, newest first, within budget."""
    try:
        journals = sorted((p for p in memory.iterdir() if _DAILY_JOURNAL.match(p.name) and p.is_file()), reverse=True)[
            :2
        ]
    except OSError:
        journals = []
    if not journals:
        return ""
    context = "# Recent Memory\n\n"
    found = False
    for path in journals:
        header = f"## {path.name}\n"
        budget = RECENT_MEMORY_MAX_BYTES - len((context + header + "\n\n").encode("utf-8"))
        if budget <= 0:
            break
        candidate = _preview_sections(path, RECENT_MEMORY_MAX_LINES)
        if not candidate:
            continue  # nothing useful in this journal; an older one may have some
        trimmed = _tail_within_bytes(candidate, budget)
        if not trimmed:
            break  # not even the newest whole line fits; older ones cannot either
        context += header + "\n".join(trimmed) + "\n\n"
        found = True
        if len(context.encode("utf-8")) >= RECENT_MEMORY_MAX_BYTES:
            break
    return context if found else ""


_EMITTED = False


def _emit(payload: dict) -> None:
    global _EMITTED
    _EMITTED = True
    click.echo(json.dumps(payload))


def _disabled() -> bool:
    return os.environ.get("MEMSEARCH_MINI_DISABLE") == "1"


def _safe(func):
    """A hook never raises: it prints a JSON object and exits 0, always."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        global _EMITTED
        _EMITTED = False
        try:
            return func(*args, **kwargs)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            if not _EMITTED:
                _emit({})
        return None

    return wrapper


def _version() -> str:
    try:
        return _dist_version("memsearch-mini")
    except Exception:
        return "dev"


def _line_count(path: Path) -> int:
    """Newlines in a file, like ``wc -l`` — the upstream emptiness heuristic."""
    total = 0
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(65536):
                total += chunk.count(b"\n")
    except OSError:
        return 0
    return total


_platform_option = click.option("--platform", type=click.Choice(["claude", "codex"]), required=True, help="Host agent.")


@click.group("hook")
def hook() -> None:
    """Hook entry points. Always exit 0 and print one JSON object."""


@hook.command("session-start")
@_platform_option
@_safe
def session_start(platform: str) -> None:
    """Status line, background reindex and recent-memory injection."""
    if _disabled():
        _emit({})
        return
    payload = read_payload()
    project_dir = resolve_project_dir(payload)
    memory = memory_dir(project_dir)
    if not config.config_path().exists():  # first run: free local embedding
        try:
            config.save({"embedding": {"provider": "onnx"}})
        except OSError:
            traceback.print_exc(file=sys.stderr)
    cfg = config.load()
    status = f"[memsearch-mini v{_version()}] embedding: {cfg.embedding.provider}/{cfg.effective_model() or 'unknown'}"
    try:
        memory.mkdir(parents=True, exist_ok=True)
    except OSError:
        traceback.print_exc(file=sys.stderr)
    problem = _provider_problem(cfg)
    if problem:  # search and indexing would fail: no reindex, no injection
        _emit({"systemMessage": status + problem})
        return
    index_status, stale = _index_state(memsearch_mini_dir(project_dir) / "index.db", memory)
    status += f" | index: {index_status} | memory: {memory}" + _summarize_warning(cfg)
    if stale:
        _spawn_detached(_index_argv(memory), project_dir, _reindex_env())
    try:
        _sweep_orphan_venvs()  # inline: a few stat calls, then an rmtree only when one is orphaned
    except Exception:
        traceback.print_exc(file=sys.stderr)
    pending = _sweep_pending(project_dir)
    if pending:
        status += f" | {pending}"
    result = {"systemMessage": status}
    context = recent_memory(memory)
    if context:
        result["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": context}
    _emit(result)


def _summarize_and_append(cfg, platform: str, turn, memory: Path, project_dir: Path, now=None) -> str:
    """Journal the turn; returns the summarizer's failure reason, or "" when it produced bullets."""
    summary, reason = capture.summarize_turn(
        cfg,
        platform,
        turn.text,
        prompt=capture.load_prompt(cfg, platform, project_dir),
        cwd=project_dir,
    )
    if not summary and platform == "codex":
        summary = capture.mechanical_summary(turn.user_question, turn.last_message, turn.text)
    if not summary.strip():
        summary = capture.unavailable_line(reason)
    capture.append_to_journal(
        memory,
        session_id=turn.session_id,
        turn_uuid=turn.turn_uuid,
        transcript_path=turn.transcript_path,
        summary=summary,
        anchor_kind="rollout" if platform == "codex" else "transcript",
        now=now,
        suffix=capture.journal_suffix(cfg),
    )
    return reason


@hook.command("stop")
@_platform_option
@_safe
def stop(platform: str) -> None:
    """Summarize the last turn and append it to today's journal."""
    if _disabled() or (platform == "codex" and os.environ.get("MEMSEARCH_MINI_IN_STOP_WORKER")):
        _emit({})
        return
    payload = read_payload()
    active = payload.get("stop_hook_active")
    if active is True or (isinstance(active, str) and active.lower() == "true"):
        _emit({})  # this Stop was triggered by a Stop hook: do not recurse
        return
    raw_path = payload.get("transcript_path")
    transcript = Path(raw_path) if isinstance(raw_path, str) and raw_path else None
    exists = transcript is not None and transcript.is_file()
    if platform == "claude" and not exists:
        _emit({})
        return
    if exists and _line_count(transcript) < 3:  # no real content
        _emit({})
        return
    cfg = config.load()
    if not cfg.agent(platform).summarize_enabled or config.missing_api_key(cfg):
        _emit({})
        return
    project_dir = resolve_project_dir(payload)
    turn = capture.extract_last_turn(
        transcript or "",
        platform,
        session_id=str(payload.get("session_id") or ""),
        last_assistant_message=str(payload.get("last_assistant_message") or ""),
    )
    if not turn.usable:
        _emit({})
        return
    memory = memory_dir(project_dir)
    if platform == "codex":
        _codex_handoff(turn, memory, project_dir)
        return
    now = datetime.now()
    pending = _pending_write(project_dir, turn, now)
    reason = _summarize_and_append(cfg, platform, turn, memory, project_dir, now=now)
    _pending_discard(pending)
    _spawn_detached(_index_argv(memory), project_dir, _reindex_env())
    # The host shows systemMessage once this async hook completes: a quiet "safe to quit now".
    message = (
        "[memsearch-mini] turn captured"
        if not reason
        else f"[memsearch-mini] turn recorded without a summary ({reason})"
    )
    recovery = _sweep_pending(project_dir)
    if recovery:
        message += f" | {recovery}"
    _emit({"systemMessage": message})


def _codex_handoff(turn, memory: Path, project_dir: Path) -> None:
    """Codex may delete the rollout on return, so parsing already happened here."""
    work = {
        "platform": "codex",
        "now": datetime.now().isoformat(),
        "project_dir": str(project_dir),
        "memory_dir": str(memory),
        "session_id": turn.session_id,
        "transcript_path": turn.transcript_path,
        "content": turn.text,
        "user_question": turn.user_question,
        "last_message": turn.last_message,
    }
    fd, name = tempfile.mkstemp(prefix="memsearch-mini-stop.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(work, handle)
    _emit({})
    sys.stdout.flush()
    _spawn_detached(
        [sys.executable, "-m", "memsearch_mini", "hook", "stop-worker", name],
        project_dir,
        {**os.environ, "MEMSEARCH_MINI_IN_STOP_WORKER": "1"},
    )


@hook.command("stop-worker", hidden=True)
@click.argument("workfile")
@_safe
def stop_worker(workfile: str) -> None:
    """Detached second phase of the Codex stop hook. Not called by hosts."""
    path = Path(workfile)
    try:
        work = _as_dict(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        work = None
    finally:
        with contextlib.suppress(OSError):
            path.unlink()  # single use: never leave the payload behind
    if not work or not work.get("content"):
        _emit({})
        return
    project_dir = Path(work.get("project_dir") or os.getcwd())
    memory = Path(work.get("memory_dir") or memory_dir(project_dir))
    turn = capture.ParsedTurn(
        text=str(work.get("content") or ""),
        session_id=str(work.get("session_id") or ""),
        transcript_path=str(work.get("transcript_path") or ""),
        user_question=str(work.get("user_question") or ""),
        last_message=str(work.get("last_message") or ""),
    )
    try:
        now = datetime.fromisoformat(work["now"]) if work.get("now") else None
    except (TypeError, ValueError):
        now = None
    _summarize_and_append(config.load(), "codex", turn, memory, project_dir, now=now)
    _run_index(memory, project_dir)
    _emit({})


@hook.command("recover", hidden=True)
@click.argument("project_dir")
@_safe
def recover(project_dir: str) -> None:
    """Detached: journal the turns whose Stop hook died mid-summary. Not called by hosts."""
    project = Path(project_dir)
    memory = memory_dir(project)
    cfg = config.load()
    suffix = capture.journal_suffix(cfg)
    recovered = 0
    for record in _stale_pending(pending_dir(project)):
        claimed = _pending_claim(record)
        if claimed is None:
            continue  # another worker got there first
        try:
            work = _as_dict(claimed.read_text(encoding="utf-8", errors="replace")) or {}
            turn_uuid = str(work.get("turn_uuid") or "")
            try:
                stamp = datetime.fromisoformat(str(work.get("now") or ""))
            except ValueError:
                stamp = datetime.now()
            turn = capture.extract_last_turn(
                str(work.get("transcript_path") or ""),
                "claude",
                session_id=str(work.get("session_id") or ""),
                turn_uuid=turn_uuid,
            )
            if turn.usable and turn.turn_uuid == turn_uuid and not _already_journaled(memory, turn_uuid, stamp, suffix):
                _summarize_and_append(cfg, "claude", turn, memory, project, now=stamp)
                recovered += 1
        except Exception:
            traceback.print_exc(file=sys.stderr)
        finally:
            _pending_discard(claimed)
    if recovered:
        _run_index(memory, project)
    _emit({"systemMessage": f"[memsearch-mini] recovered {recovered} turn(s)"})  # lands in index.log
