"""Tests for the click CLI: command set, output shapes and exit codes.

Every test runs against the deterministic fake embedder from ``conftest`` — the
suite must never download a model or reach the network — and pins project
resolution to ``tmp_path`` with ``CLAUDE_PROJECT_DIR`` so the index and the
journals land in a throwaway directory.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from memsearch_mini import cli as cli_module
from memsearch_mini.cli import cli

JOURNAL_OLD = """# 2026-09-08

## Session 09:00

### 09:00
<!-- session:s-one turn:t-abc transcript:/tmp/claude/s-one.jsonl -->
- Edgar chose SQLite with FTS5 for the derived index.
- Claude Code dropped the vector server dependency.

## Session 11:30

### 11:30
<!-- session:s-two rollout:/tmp/codex/rollout-two.jsonl -->
- Edgar asked how the hook launchers detach their children.
"""

JOURNAL_NEW = """# 2026-09-09

## Session 08:15

### 08:15
- Edgar reviewed the embedding provider defaults for ONNX.
"""

CLAUDE_TRANSCRIPT = [
    {"type": "user", "uuid": "u-one", "message": {"role": "user", "content": "which store did we pick?"}},
    {
        "type": "assistant",
        "uuid": "a-one",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "SQLite with FTS5."},
                {"type": "tool_use", "id": "call-1", "name": "Bash", "input": {"command": "sqlite3 --version"}},
            ],
        },
    },
    {
        "type": "user",
        "uuid": "u-two",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "3.37.2"}]},
    },
]


def run(*args: str, **kwargs):
    """Invoke the CLI; unexpected exceptions surface instead of becoming exit 1."""
    return CliRunner().invoke(cli, list(args), catch_exceptions=False, **kwargs)


@pytest.fixture
def wired(tmp_path, monkeypatch, fake_embedder):
    """A project directory the CLI resolves to, wired to the fake embedder."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(cli_module, "_embedder_for", lambda *args, **kwargs: fake_embedder)
    return tmp_path


@pytest.fixture
def project(wired):
    """*wired*, with two daily journals already in the memory directory."""
    memory = wired / ".memsearch-mini" / "memory"
    memory.mkdir(parents=True)
    (memory / "2026-09-08.md").write_text(JOURNAL_OLD, encoding="utf-8")
    (memory / "2026-09-09.md").write_text(JOURNAL_NEW, encoding="utf-8")
    return wired


def hits_for(query: str, top_k: int = 20) -> list[dict]:
    return json.loads(run("search", query, "-k", str(top_k), "--json").stdout)


def chunk_containing(text: str) -> dict:
    """The indexed chunk whose content holds *text* (searching is how we find it)."""
    return next(hit for hit in hits_for(text) if text in hit["content"])


# -- command surface -------------------------------------------------------------


def test_help_lists_exactly_the_documented_commands() -> None:
    result = run("--help")

    assert result.exit_code == 0
    listed = {line.split()[0] for line in result.stdout.partition("Commands:")[2].splitlines() if line.startswith("  ")}
    assert listed == {"config", "expand", "hook", "index", "reset", "search", "stats", "transcript"}


def test_version_reports_the_package_version() -> None:
    result = run("--version")

    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_python_dash_m_runs_the_cli() -> None:
    proc = subprocess.run([sys.executable, "-m", "memsearch_mini", "--version"], capture_output=True)

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "0.1.0" in proc.stdout.decode("utf-8")


# -- index -----------------------------------------------------------------------


def test_index_summarises_the_run_and_stats_agrees(project) -> None:
    result = run("index")

    assert result.exit_code == 0
    assert result.stdout.strip() == "Indexed 3 chunks from 2 files"
    stats = run("stats").stdout
    assert f"Index: {project / '.memsearch-mini' / 'index.db'}" in stats
    assert "Chunks: 3" in stats
    assert "Sources: 2" in stats
    assert "Embedding: onnx/fake-embed (dimension 8)" in stats
    assert "Last indexed: 20" in stats  # an ISO stamp, not "never"
    assert f"Memory dir: {project / '.memsearch-mini' / 'memory'}" in stats


def test_index_creates_the_memory_directory_when_missing(wired) -> None:
    result = run("index")

    assert result.exit_code == 0
    assert result.stdout.strip() == "Indexed 0 chunks from 0 files"
    assert (wired / ".memsearch-mini" / "memory").is_dir()


def test_index_is_incremental_and_force_re_embeds(project) -> None:
    run("index")

    assert run("index").stdout.strip() == "Indexed 0 chunks from 2 files"
    assert run("index", "--force").stdout.strip() == "Indexed 3 chunks from 2 files"


def test_index_accepts_explicit_paths(project) -> None:
    result = run("index", str(project / ".memsearch-mini" / "memory" / "2026-09-09.md"))

    assert result.exit_code == 0
    assert result.stdout.strip() == "Indexed 1 chunks from 1 files"


def test_index_rejects_a_missing_path(project) -> None:
    result = CliRunner().invoke(cli, ["index", str(project / "nowhere.md")])

    assert result.exit_code == 2  # click usage error


def test_index_reports_failed_files_and_exits_one(project, monkeypatch) -> None:
    monkeypatch.setenv("MEMSEARCH_MINI_MAX_FILE_MB", "0.00001")

    result = run("index")

    assert result.exit_code == 1
    assert result.stdout.strip() == "Indexed 0 chunks from 2 files (2 files failed)"
    assert "2026-09-08.md: ValueError: file is" in result.stderr


def test_index_skips_a_locked_project_and_releases_its_own_lock(project) -> None:
    lock = project / ".memsearch-mini" / "index.lock"
    with open(lock, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        skipped = run("index", "--skip-if-locked")

    assert skipped.exit_code == 0
    assert (skipped.stdout, skipped.stderr) == ("", "")
    assert "Chunks: 0" in run("stats").stdout
    # Would block forever if the previous run had leaked its own lock.
    assert run("index").stdout.strip() == "Indexed 3 chunks from 2 files"
    assert run("index", "--skip-if-locked").stdout.strip() == "Indexed 0 chunks from 2 files"


# -- search ----------------------------------------------------------------------


def test_search_prints_human_readable_results(project) -> None:
    run("index")

    result = run("search", "SQLite FTS5 derived index", "-k", "2")

    assert result.exit_code == 0
    assert "--- Result 1 (score: 0." in result.stdout
    assert f"Source: {project / '.memsearch-mini' / 'memory' / '2026-09-08.md'}" in result.stdout
    assert "Heading: 09:00" in result.stdout
    assert "- Edgar chose SQLite with FTS5 for the derived index." in result.stdout
    assert result.stdout.count("--- Result ") <= 2


def test_search_json_uses_the_documented_key_order(project) -> None:
    run("index")

    payload = json.loads(run("search", "SQLite FTS5", "--json").stdout)

    assert payload, "the fixture journals must produce at least one hit"
    assert list(payload[0]) == ["content", "source", "heading", "heading_level",
                                "start_line", "end_line", "chunk_id", "score"]  # fmt: skip
    assert 0.0 < payload[0]["score"] <= 1.0


def test_search_truncates_long_chunks_and_points_at_expand(project) -> None:
    long_section = "# 2026-09-10\n\n## Session 10:00\n\n### 10:00\n" + "- lorem ipsum dolor sit amet elit\n" * 40
    (project / ".memsearch-mini" / "memory" / "2026-09-10.md").write_text(long_section, encoding="utf-8")
    run("index")

    output = run("search", "lorem ipsum dolor").stdout

    assert "... [truncated, run 'memsearch-mini expand " in output


def test_search_on_an_empty_index_says_so(wired) -> None:
    assert run("search", "anything").stdout.strip() == "No results (index is empty)"
    assert run("search", "anything", "--json").stdout.strip() == "[]"


def test_search_uses_the_identity_the_index_was_built_with(project, monkeypatch, fake_embedder) -> None:
    run("index")
    run("config", "set", "embedding.provider", "openai")
    seen: dict[str, str] = {}

    def recorder(cfg, provider: str = "", model: str = ""):
        seen.update(provider=provider, model=model)
        return fake_embedder

    monkeypatch.setattr(cli_module, "_embedder_for", recorder)
    result = run("search", "SQLite")

    assert result.exit_code == 0
    assert seen == {"provider": "onnx", "model": "fake-embed"}


# -- expand ----------------------------------------------------------------------


def test_expand_returns_the_section_and_a_transcript_anchor(project) -> None:
    run("index")
    hit = chunk_containing("Edgar chose SQLite with FTS5")

    payload = json.loads(run("expand", hit["chunk_id"], "--json").stdout)

    assert payload["chunk_id"] == hit["chunk_id"]
    assert payload["source"] == hit["source"]
    assert payload["content"].startswith("## Session 09:00")
    assert "- Claude Code dropped the vector server dependency." in payload["content"]
    assert "## Session 11:30" not in payload["content"]  # stops at the next sibling heading
    assert payload["start_line"] < hit["start_line"]
    assert payload["anchor"] == {"session": "s-one", "turn": "t-abc",
                                 "kind": "transcript", "transcript": "/tmp/claude/s-one.jsonl"}  # fmt: skip


def test_expand_parses_a_codex_rollout_anchor(project) -> None:
    run("index")
    hit = chunk_containing("hook launchers detach")

    payload = json.loads(run("expand", hit["chunk_id"], "--json").stdout)

    assert payload["anchor"] == {"session": "s-two", "turn": "",
                                 "kind": "rollout", "transcript": "/tmp/codex/rollout-two.jsonl"}  # fmt: skip


def test_expand_reports_a_missing_anchor_as_null(project) -> None:
    run("index")
    hit = chunk_containing("embedding provider defaults")

    assert json.loads(run("expand", hit["chunk_id"], "--json").stdout)["anchor"] is None


def test_expand_human_output_shows_source_heading_and_anchor(project) -> None:
    run("index")
    hit = chunk_containing("Edgar chose SQLite with FTS5")

    output = run("expand", hit["chunk_id"]).stdout

    assert output.startswith(f"Source: {hit['source']} (lines ")
    assert "Heading: 09:00" in output
    assert "Anchor: session:s-one turn:t-abc transcript:/tmp/claude/s-one.jsonl" in output
    assert "### 09:00" in output


def test_expand_lines_mode_windows_around_the_chunk(project) -> None:
    run("index")
    hit = chunk_containing("Edgar chose SQLite with FTS5")
    total = len(Path(hit["source"]).read_text(encoding="utf-8").splitlines())

    payload = json.loads(run("expand", hit["chunk_id"], "--lines", "2", "--json").stdout)

    assert payload["start_line"] == max(1, hit["start_line"] - 2)
    assert payload["end_line"] == min(total, hit["end_line"] + 2)
    assert payload["content"].splitlines()[0] == "## Session 09:00"


def test_expand_rejects_an_unknown_chunk_id(project) -> None:
    run("index")

    result = run("expand", "deadbeefdeadbeef")

    assert result.exit_code == 1
    assert "Chunk not found: deadbeefdeadbeef" in result.stderr


# -- transcript ------------------------------------------------------------------


def test_transcript_renders_turns_with_tool_calls(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in CLAUDE_TRANSCRIPT) + "\n", encoding="utf-8")

    result = run("transcript", str(path))

    assert result.exit_code == 0
    assert "### User" in result.stdout
    assert "which store did we pick?" in result.stdout
    assert "- $ [Bash] sqlite3 --version" in result.stdout
    assert "→ 3.37.2" in result.stdout


def test_transcript_selects_a_turn_with_context(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in CLAUDE_TRANSCRIPT) + "\n", encoding="utf-8")

    payload = json.loads(run("transcript", str(path), "--turn", "u-one", "--context", "0", "--json").stdout)

    assert [turn["uuid"] for turn in payload] == ["u-one"]
    assert payload[0]["role"] == "user"
    assert payload[0]["tools"] == []


def test_transcript_exits_three_on_an_unknown_format(tmp_path) -> None:
    path = tmp_path / "mystery.jsonl"
    path.write_text('{"hello": "world"}\n{"another": 1}\n', encoding="utf-8")

    result = run("transcript", str(path))

    assert result.exit_code == 3
    assert "Unrecognized transcript format" in result.stderr


def test_transcript_exits_one_when_the_file_is_missing(tmp_path) -> None:
    result = run("transcript", str(tmp_path / "gone.jsonl"))

    assert result.exit_code == 1
    assert "transcript not found" in result.stderr


# -- config ----------------------------------------------------------------------


def test_config_get_prints_defaults_and_booleans(wired) -> None:
    assert run("config", "get", "embedding.provider").stdout.strip() == "onnx"
    assert run("config", "get", "chunking.max_chunk_size").stdout.strip() == "1500"
    assert run("config", "get", "claude.summarize_enabled").stdout.strip() == "true"


def test_config_set_coerces_and_persists(wired) -> None:
    assert run("config", "set", "chunking.max_chunk_size", "800").stdout.strip() == "chunking.max_chunk_size = 800"
    assert run("config", "get", "chunking.max_chunk_size").stdout.strip() == "800"
    assert run("config", "set", "codex.summarize_enabled", "no").stdout.strip() == "codex.summarize_enabled = false"
    assert run("config", "get", "codex.summarize_enabled").stdout.strip() == "false"


@pytest.mark.parametrize(
    "args",
    [
        ["config", "get", "embedding.nope"],
        ["config", "get", "nosection"],
        ["config", "set", "embedding.nope", "x"],
        ["config", "set", "chunking.overlap_lines", "two"],
    ],
)
def test_config_rejects_bad_keys_and_values(wired, args: list[str]) -> None:
    result = run(*args)

    assert result.exit_code == 1
    assert result.stderr.startswith("Error: ")
    assert not result.stderr.startswith("Error: '")  # a KeyError message, not its repr


def test_config_list_renders_every_section(wired) -> None:
    run("config", "set", "embedding.model", "custom-model")

    lines = run("config", "list").stdout.splitlines()

    assert "embedding.provider = onnx" in lines
    assert "embedding.model = custom-model" in lines
    assert "chunking.overlap_lines = 2" in lines
    assert "claude.summarize_model = haiku" in lines
    assert "prompts.summarize = " in lines


def test_config_list_json_is_nested(wired) -> None:
    payload = json.loads(run("config", "list", "--json").stdout)

    assert payload["embedding"]["provider"] == "onnx"
    assert payload["chunking"]["max_chunk_size"] == 1500
    assert payload["codex"]["summarize_enabled"] is True


# -- reset -----------------------------------------------------------------------


def test_reset_empties_the_index(project) -> None:
    run("index")

    result = run("reset", "--yes")

    assert result.exit_code == 0
    assert result.stdout.strip() == "Index cleared"
    assert "Chunks: 0" in run("stats").stdout
    assert run("index").stdout.strip() == "Indexed 3 chunks from 2 files"


def test_reset_asks_for_confirmation(project) -> None:
    run("index")

    result = CliRunner().invoke(cli, ["reset"], input="n\n")

    assert result.exit_code == 1  # click.confirmation_option aborts
    assert "Chunks: 3" in run("stats").stdout


def test_search_and_expand_keep_non_ascii_content(project) -> None:
    (project / ".memsearch-mini" / "memory" / "2026-09-11.md").write_text(
        "# 2026-09-11\n\n## Session 12:00\n\n### 12:00\n- Edgar perguntou sobre 非拉丁字符 e Amyloid-β.\n",
        encoding="utf-8",
    )
    run("index")
    hit = chunk_containing("非拉丁字符")

    payload = json.loads(run("expand", hit["chunk_id"], "--json").stdout)

    assert "Amyloid-β" in hit["content"]
    assert "非拉丁字符" in payload["content"]
    assert "非拉丁字符" in run("expand", hit["chunk_id"]).stdout
