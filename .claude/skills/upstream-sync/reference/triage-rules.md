# Triage rules

## What the fork still has (a report against these is worth reading closely)

| Upstream area | Fork counterpart | Notes |
|---|---|---|
| `src/memsearch/chunker.py` | same file, kept verbatim except `compute_chunk_id` (no model component) | CJK splitting, heading handling, size limits all apply |
| `src/memsearch/embeddings/*` | same files | provider bugs, dimension probing, batching, ONNX inputs apply; `get_provider` forwards kwargs by signature introspection here |
| `src/memsearch/transcript.py` | same file minus OpenClaw, plus harness-tag stripping | L3 deep-drill; Claude + Codex formats |
| `src/memsearch/config.py` | rewritten: one file `~/.memsearch-mini/config.toml`, no project layer, no `env:` refs | only the concept of a config bug transfers |
| `src/memsearch/store.py`, `core.py` | rewritten: SQLite + FTS5 + numpy, `index_paths()` | search/index *semantics* bugs (stale chunks, deleted files, dedup, score normalisation) transfer; anything about Milvus does not |
| `src/memsearch/cli.py` | rewritten, 9 commands | flag names differ; check the fork's `--help` before assuming a flag exists |
| `plugins/claude-code/hooks/*.sh`, `plugins/codex/hooks/*.sh`, `parse-transcript.sh`, `parse-rollout.sh` | `hooks/*.sh` (thin launchers) + `src/memsearch_mini/hooks.py` + `capture.py` | hook *behaviour* bugs transfer (stdin handling, recursion guards, journal format, summarizer invocation, project-root resolution); bash-specific bugs usually vanished with the rewrite — verify before recording `already-covered` |
| `plugins/claude-code/skills/memory-recall`, `plugins/codex/skills/memory-recall` | `skills/memory-recall`, `codex/skills/memory-recall` | |
| `plugins/_shared/prompts/summarize.txt` | `prompts/summarize.txt` | |
| `plugins/codex/scripts/install.sh` | `codex/install.sh`, `uninstall.sh` | |
| `README.md`, `docs/platforms/{claude-code,codex}` | `README.md` only | port a doc fix only if the fork's README makes the same claim |

Paths in the first column are upstream's. This fork's package is `src/memsearch_mini/`, so an
upstream `src/memsearch/<module>.py` is `src/memsearch_mini/<module>.py` here.

## What the fork removed (reports against these are `not-applicable`)

OpenCode, OpenClaw, DeepSeek Harness, Hermes, Kimi, ZCode, pi, VSCode Copilot, any new platform;
Milvus Lite / Server / Zilliz Cloud, collections, `--default-collection`, `milvus.*` config;
`watch`, `watcher.py`, PID files, orphan sweeps (`pgrep`, `setsid` daemons); maintenance
(`PROJECT.md`, `USER.md`, `maintenance-runner.py`), memory-to-skill (`skills.py`, skill
candidates), `compact`, reranker, `[llm]` providers and `memsearch summarize`, `index_state.py`,
`.index-state.json`, `sync-skills.sh`/`sync-prompts.sh`, the Python API (`MemSearch` class),
project-level `.memsearch.toml`, `env:` config indirection, PyPI/uvx/version probing, `uv`
self-install, mkdocs site, release workflows, Windows.

A report that mentions one of these by name can still describe a *concept* the fork shares (for
example "watch pidfile inside the watched dir breaks worktrees" is about where state lives, and the
fork keeps `index.lock` inside `.memsearch-mini/`). Read the concept, then decide.

## Verdict procedure

1. Identify the concept: what actually goes wrong, for whom, under which condition.
2. Locate the fork counterpart with the map above. None → `not-applicable`.
3. Reproduce or reason it through against the fork's code. Fork already behaves correctly (by
   design or by the rewrite) → `already-covered`, with the line of code or test that proves it.
4. Real bug in the fork → `applied`, through the trail in `reference/apply-flow.md`: issue here,
   fix in the fork's own idiom with a test that fails before and passes after, full suite, commit,
   push, closure.
5. A small feature that would fit the fork (a config knob, a flag, a behaviour users of this
   plugin plausibly want) → `ask-user` with a short proposal (what, why, cost in lines/deps, what
   happens if not done). Do not implement while waiting. A feature that is a whole subsystem, a
   new platform, a new provider family or a new dependency → `rejected` on scope, one line in the
   report's "features not taken" list so the user can override.
6. Applies, but the only fix adds machinery out of proportion to the harm → `rejected`, say why.
7. Upstream itself is undecided, or the report lacks a reproduction → `deferred`; it will be
   re-listed next run.

## Pull requests

- Judge the *problem the PR solves*, then decide independently how the fork should solve it. The
  PR's tests are often the most useful part: port the test, then make it pass in the fork's way.
- A PR whose files are all docs / other platforms / removed subsystems is `not-applicable` from the
  `scope_hint` alone; no need to read the body. A `docs-or-other` hint still deserves a glance at
  the title: `README.md` and `.github/workflows/test.yml` exist here.
- **Stacked PRs** (a series where each PR contains its predecessors, recognisable by monotonically
  growing file counts and one author): judge the series by its distinct concepts, record one
  verdict per PR anyway, and point the later entries at the entry that carries the decision with
  `related`.
- Test-only PRs: `already-covered` when the fork's tests pin the behaviour (name the test),
  `not-applicable` when they test a removed module, `rejected` as "coverage-only" when they add no
  behaviour, `applied` only when the upstream test exposes a real gap.
- Drafts get judged like any other PR; note `draft` in the reason if it matters.
- Merged-since-baseline PRs are the most likely to apply: upstream accepted them.

## Writing the reason

One or two sentences a future reader can trust without reopening the item: the concept, the fork
counterpart (file or "none"), and why the verdict follows. For `applied`, `action` names the files
and the test, and the entry carries the fork issue and commit.
