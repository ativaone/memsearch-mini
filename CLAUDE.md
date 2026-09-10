# CLAUDE.md

Guidance for Claude Code working in this repository.

## Commands

```bash
uv sync --extra onnx --group dev              # dev environment (a ./.venv here; the plugin never makes one)
uv run python -m pytest                       # full suite
uv run python -m pytest tests/test_store.py -v
uv run ruff check src tests
uv run ruff format --check src tests

bin/memsearch-mini --sync                          # blocking sync of the plugin runtime
bin/memsearch-mini search "query" -k 5 --json      # the CLI exactly as the hooks call it
```

`uv` is a prerequisite. Nothing in this repo installs it, and nothing here may add a step that does.

## Architecture

The repository root **is** the Claude Code plugin root: `.claude-plugin/` holds the manifests, and
`hooks/hooks.json` plus `skills/` are auto-discovered from here. `codex/install.sh` wires the same
scripts into `~/.codex/hooks.json` and `~/.agents/skills/`.

`hooks/*.sh` are launchers, not logic: check the kill switch, check `uv`, check the runtime, then
`exec bin/memsearch-mini hook <event> --platform <p>` with stdin untouched so Python reads the payload.
`hooks/common.sh` is a sourced library with no side effects. `bin/memsearch-mini` runs
`uv run --project <root> --frozen --no-sync` against this checkout, so the hooks and the CLI they
call always come from the same tree.

Markdown journals under `<project>/.memsearch-mini/memory/` are the source of truth. `index.db` beside
them is a derived SQLite index — chunk rows, float32 embeddings and an FTS5 table, fused with RRF —
one database per project, rebuildable from the markdown at any time. `capture.py` extracts the last
turn, summarizes it through `claude -p` / `codex exec` and appends it to today's journal; `hooks.py`
is the `hook` command group and inspects the index with stdlib `sqlite3` alone. Recall is the
`memory-recall` skill (`context: fork` for Claude, `__INSTALL_DIR__`-substituted for Codex):
search → expand → transcript. Runtime state lives under `$MEMSEARCH_MINI_HOME` (default `~/.memsearch-mini`).

## Rules

- **Never write outside `$MEMSEARCH_MINI_HOME` and `<project>/.memsearch-mini`.** The plugin checkout is
  read-only at runtime: no virtualenv, no cache, no state inside it.
- **Never move `bin/`, `hooks/`, `skills/` or `.claude-plugin/` into a subdirectory.** The root is
  the plugin root and `marketplace.json` points at `"./"`.
- **One version, three files:** `pyproject.toml`, `.claude-plugin/plugin.json` and
  `.claude-plugin/marketplace.json` (both `metadata.version` and the plugin entry). Bump them
  together — `tests/test_packaging.py` fails if they drift.
- **A launcher prints exactly one JSON object and exits 0.** No `set -e` anywhere in the plugin
  shell, and every function ends with an explicit `return`.
- **Every detached child redirects all three fds** (`</dev/null >>"$LOG" 2>&1 &` in bash;
  `stdin=DEVNULL`, stdout/stderr to `$MEMSEARCH_MINI_HOME/index.log`, `start_new_session=True` in
  Python). Otherwise the host keeps waiting on a pipe it still holds open.
- **The CLI comes from this checkout, full stop.** Never fall back to a published package, and never
  probe the network for a newer version.
- **Tests isolate `HOME` and never touch `~`.** `conftest.isolated_home` repoints `HOME`,
  `MEMSEARCH_MINI_CONFIG` and `TMPDIR` into `tmp_path` and clears the `MEMSEARCH_MINI_*` switches; keep it that
  way for any new test.
- **Upstream fixes arrive through the `upstream-sync` skill** (`.claude/skills/upstream-sync/`): one
  issue here per ported item, a commit that references it, its closure, and a catalog entry in
  `.claude/upstream-sync/`. Never merge or cherry-pick from upstream.
- **Hooks stay import-light.** No `numpy`, `onnxruntime` or `memsearch_mini.store` at module scope in
  `hooks.py` — a SessionStart that pays for an ONNX import blows its 10-second budget.
