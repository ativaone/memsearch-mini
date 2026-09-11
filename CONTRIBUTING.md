# Contributing to memsearch-mini

Thanks for your interest in contributing! memsearch-mini is a reduced-scope fork of
[zilliztech/memsearch](https://github.com/zilliztech/memsearch) — local SQLite instead of Milvus,
two host platforms (Claude Code and Codex CLI) instead of four, and the repository root doubling
as the Claude Code plugin root. Keep that scope in mind: features that widen it need an issue
first.

## Getting Started

```bash
git clone https://github.com/ativaone/memsearch-mini.git
cd memsearch-mini
uv sync --extra onnx --group dev
uvx pre-commit install
```

> **Dependency management:** use `uv` and `pyproject.toml` — never `pip install` directly.
>
> **Pre-commit hooks:** the `pre-commit install` step registers Git hooks that run
> `ruff check --fix` and `ruff format` on staged files before each commit (`pre-commit` itself is
> not a project dependency, hence `uvx`).

## Running Tests

```bash
# Full suite
uv run python -m pytest

# Single file
uv run python -m pytest tests/test_store.py -v

# Single test
uv run python -m pytest tests/test_cli.py::test_expand_stays_inside_the_chunks_own_turn -v
```

> **Note:** always `uv run python -m pytest`, not `uv run pytest`, to avoid picking up a
> system-level pytest.

Tests never touch your real `~`: `conftest.isolated_home` repoints `HOME`, `MEMSEARCH_MINI_CONFIG`
and `TMPDIR` into `tmp_path`. Any new test must keep that isolation. A bug fix lands with a test
that reproduces the bug first.

## Code Style

[Ruff](https://docs.astral.sh/ruff/) lints and formats; both checks must pass.

```bash
uv run ruff check src tests
uv run ruff format --check src tests
```

- Python 3.10+ with `from __future__ import annotations` for type hints.
- Code and comments in English.
- Line length limit: 120 characters.

## Commits and Pull Requests

Use [Conventional Commits](https://www.conventionalcommits.org/) prefixes for **commit messages
and PR titles**. Keep messages concise: a subject line, plus at most one body line.

| Prefix       | Example                                                  |
|--------------|----------------------------------------------------------|
| `feat:`      | `feat: add date filtering to search`                     |
| `fix:`       | `fix: skip dangling symlinks in the memory scan`         |
| `docs:`      | `docs: user upgrade instructions in README`              |
| `chore:`     | `chore: bump version to 0.1.2`                           |
| `refactor:`  | `refactor: extract the FTS drift probe`                  |
| `test:`      | `test: cover the --lines anchor path`                    |
| `ci:`        | `ci: update GitHub Actions versions`                     |

### Workflow

1. **Fork and branch.** Create a feature branch from `main`.
2. **Make your changes.** Keep PRs focused — one feature or fix per PR.
3. **Write tests.** Add or update tests in `tests/` for any new or changed behavior.
4. **Run checks.** `ruff check`, `ruff format --check` and the full pytest suite must pass.
5. **Open a PR** with a conventional prefix in the title.

House practice for defects: each fixed bug gets its own GitHub issue documenting symptom, root
cause, fix and tests, closed with a comment naming the commit — so the tracker stays a readable
history of what actually went wrong.

## Relationship with Upstream

Upstream fixes and features are ported **by concept, case by case** — never by merge or
cherry-pick. The `upstream-sync` skill (`.claude/skills/upstream-sync/`) drives it: one issue here
per ported item, a commit that references it, and a verdict recorded in `.claude/upstream-sync/`
so nothing is analyzed twice. If you spot an upstream fix that applies here, open an issue linking
it rather than porting the diff.

## Project Structure

```
.claude-plugin/             # Plugin + marketplace manifests (root IS the plugin root)
bin/memsearch-mini          # CLI entry point: uv run against this checkout
hooks/                      # Claude Code / Codex launchers + common.sh (shell, no logic)
skills/memory-recall/       # Recall skill for Claude Code
codex/                      # Codex installer + skill variant
src/memsearch_mini/
├── cli.py                  # Click CLI (index, search, expand, transcript, config)
├── store.py                # SQLite store: embeddings + FTS5, fused with RRF
├── chunker.py              # Markdown heading-based chunking
├── capture.py              # Turn capture + LLM/mechanical summarization
├── hooks.py                # `hook` command group (import-light on purpose)
├── transcript.py           # Host transcript parsing (L3 recall)
├── scanner.py / config.py  # File discovery, layered TOML config
└── embeddings/             # Pluggable providers (onnx default, openai, google, …)
tests/                      # pytest suite (HOME-isolated)
uninstall.sh                # Unwires hooks/skill, optionally purges the runtime home
```

The load-bearing invariants (launcher JSON contract, write boundaries, version-bump files, hook
import budget) live in [CLAUDE.md](CLAUDE.md) — read it before touching `hooks/`, `bin/` or the
manifests.

## Plugin Development

```bash
claude --plugin-dir /path/to/memsearch-mini   # Claude Code: test a working tree directly
bash codex/install.sh                          # Codex: wire this checkout into ~/.codex
```

Hook script edits take effect on the next hook trigger — no restart needed. Installed copies
(Claude's plugin cache, `~/.codex`, `~/.agents`) are deployment artifacts: changes land in this
repository and reach installs via update/reinstall.

## Reporting Issues

Open an issue on [GitHub](https://github.com/ativaone/memsearch-mini/issues) with:

- What you expected vs. what actually happened
- Steps to reproduce
- Python version and OS

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE).
