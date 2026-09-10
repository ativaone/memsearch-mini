# memsearch-mini

> ### About this fork
>
> memsearch-mini is a reduced fork of [zilliztech/memsearch](https://github.com/zilliztech/memsearch)
> (MIT). Upstream is a cross-platform memory system for five agent hosts, with a public Python API,
> a Milvus backend that can point at a local file, a server or Zilliz Cloud, and a stack of optional
> subsystems on top. This fork keeps one job — persistent memory for **Claude Code and Codex** on a
> single machine — and deletes everything that only existed to support the rest.
>
> - **Two hosts only.** The OpenCode, OpenClaw and DeepSeek Harness plugins are gone.
> - **SQLite + numpy instead of Milvus.** No server, no Zilliz Cloud, no orphaned `milvus_lite`
>   processes, and no collections: one `index.db` per project, derived from the markdown.
> - **No public Python API.** The package exists to back the CLI the hooks call.
> - **No background maintenance** (`PROJECT.md` / `USER.md`), no memory-to-skill distillation, no LLM
>   compaction of chunks, no cross-encoder reranker, no file watcher.
> - **The bash hooks are thin launchers.** They check the kill switch and the runtime, then hand
>   stdin to Python; all logic lives in `src/memsearch/hooks.py` and `capture.py`.
> - **The CLI runs from this checkout via `uv run`**, not from a package installed from PyPI, so the
>   hooks and the CLI they call can never come from different installs.
> - **Uninstalling leaves nothing behind.** Upstream's hooks installed `uv` for you with
>   `curl -LsSf https://astral.sh/uv/install.sh | sh`, ran the CLI as
>   `uvx --from "memsearch[onnx]" memsearch` (which warms uv's cache with the PyPI package), and let
>   the ONNX model download into the global Hugging Face cache. Uninstalling the plugin removed none
>   of that.
> - This fork installs no tools on your behalf — `uv` is a documented prerequisite — and points the
>   runtime, the uv cache, any downloaded interpreter and the model cache at `~/.memsearch`.
>   Removing the plugin and that one directory removes everything.
>
> Everything that survived — the journal format, the chunker, the embedding providers, the
> progressive-disclosure recall skill — is upstream's work, kept under the same MIT license.

Your agent forgets everything when the session ends. memsearch-mini writes each turn down as
markdown, indexes it locally, and hands the relevant parts back the next time they matter.

## What it does

- **Every session starts with context.** SessionStart prints an index status line, injects the most
  recent journal entries into the conversation, and kicks off a background reindex when the index is
  missing or older than the journals.
- **Every turn is written down.** The Stop hook extracts the last exchange, has `claude -p` (or
  `codex exec`) rewrite it as third-person bullets, and appends it to
  `.memsearch/memory/YYYY-MM-DD.md`.
- **Recall is a skill, not an injection.** `memory-recall` runs in a forked subagent that searches,
  expands the promising hits, and can drill into the original transcript — the main conversation
  only sees the curated answer.
- **One SQLite index per project**, holding chunks, embeddings and an FTS5 keyword index. It is
  derived: delete it and the next index run rebuilds it from the markdown.
- **Nothing leaves the machine** with the default `onnx` provider: embeddings are computed locally by
  onnxruntime. Only the summarizer talks to a network, and that is the agent CLI you already run.

## Requirements

- **[uv](https://docs.astral.sh/uv/)** — the plugin runs its CLI through `uv run`. It is a
  prerequisite and is never installed for you. Hooks look for it on `PATH` plus `~/.local/bin`,
  `~/bin`, `/usr/local/bin` and `/opt/homebrew/bin`. No system Python is required: uv resolves the
  interpreter.
- **`git`** — the project root is `git rev-parse --show-toplevel` when the working directory is in a
  repository.
- **`claude` and/or `codex`** — whichever host you use also does the summarizing.
- **Disk** — the first index downloads the ONNX embedding model (`gpahal/bge-m3-onnx-int8`, several
  hundred MB) into `~/.memsearch/models/`, plus roughly 90 MB of wheels in the runtime environment
  under `~/.memsearch/venvs/`.
- **POSIX** (Linux, macOS, WSL): the hooks rely on `select` on a pipe and detached process groups.

## Install — Claude Code

```
/plugin marketplace add edgarrc/memsearch-mini
/plugin install memsearch-mini@edgarrc
```

The first session prints:

```
[memsearch] installing runtime in the background — memory available from the next session
```

A detached `uv sync` is building the runtime under `~/.memsearch/venvs/`, logging to the `.log` file
beside it. When it finishes it runs the SessionStart hook once, so the model download and the first
index build start immediately instead of waiting for the next session. Until the runtime is ready,
the Stop and UserPromptSubmit hooks print `{}` and do nothing — that first session is not captured,
on purpose, because a 90 MB download does not belong inside a hook timeout.

For local development, point Claude Code at a working tree instead of the marketplace:

```bash
claude --plugin-dir /path/to/memsearch-mini
```

## Install — Codex

```bash
git clone https://github.com/edgarrc/memsearch-mini.git
bash memsearch-mini/codex/install.sh
```

The installer runs five steps: it checks for `uv`; runs a blocking `bin/memsearch --sync`; copies
`codex/skills/memory-recall` to `~/.agents/skills/memory-recall`; backs up `~/.codex/hooks.json` to
`~/.codex/hooks.json.bak` and merges in three entries (SessionStart 10 s, UserPromptSubmit 5 s,
Stop 30 s); sets `hooks = true` under `[features]` in `~/.codex/config.toml`; and marks the scripts
executable. Set `MEMSEARCH_SKIP_SYNC=1` to skip the blocking sync and let the first session do it.

The checkout path is baked into both `~/.codex/hooks.json` and the installed skill, so **re-run the
installer after moving or renaming the clone**. It is idempotent: it strips its own old entries
(matching any `/hooks/<script>`) as well as upstream's (`plugins/codex/hooks/<script>`) before
writing, and leaves unrelated hooks alone.

Codex sandboxing: the first index needs network access to download the embedding model. Upstream's
README recommended running that first session with full access
(`codex --dangerously-bypass-approvals-and-sandbox`); after the model is cached, a read-only sandbox
is enough.

To undo all of this, see [Uninstall](#uninstall).

## How it works

```
turn ends ─▶ Stop hook ─▶ parse last turn ─▶ claude -p / codex exec ─▶ bullets
                                                                        │
              .memsearch/memory/YYYY-MM-DD.md  ◀───────────────────────┘
                            │
              index (detached) ─▶ chunk ─▶ embed ─▶ .memsearch/index.db
                                                          │
  question ─▶ memory-recall skill ─▶ search (dense + FTS5, fused with RRF)
                                       └─▶ expand ─▶ transcript
```

### On disk

Two places, and only two: the project, and `$MEMSEARCH_HOME` (default `~/.memsearch`). **The plugin
checkout is never written to at runtime** — no virtualenv inside it, no caches, no state.

Per project:

| Path | What it is |
|---|---|
| `<project>/.memsearch/memory/YYYY-MM-DD.md` | Daily journal — **the source of truth**. Plain markdown, editable, versionable. |
| `<project>/.memsearch/index.db` | Derived SQLite index: chunk rows, float32 embeddings, an FTS5 table. Rebuildable. |
| `<project>/.memsearch/index.lock` | Held by a running indexer; `index --skip-if-locked` gives up instead of queueing. |

Under `$MEMSEARCH_HOME`:

| Path | What it is |
|---|---|
| `~/.memsearch/config.toml` | The only configuration layer. |
| `~/.memsearch/venvs/<hash>/` | The runtime, created and kept current by `uv` (`UV_PROJECT_ENVIRONMENT`). The hash comes from the plugin path, so a Claude Code install and a Codex clone get one each. |
| `~/.memsearch/venvs/<hash>.log` · `<hash>.lock` | Sync log and sync lock — *siblings* of the environment, never inside it. |
| `~/.memsearch/uv-cache/` | uv's package cache (`UV_CACHE_DIR`). |
| `~/.memsearch/python/` | Interpreter uv downloaded, if it had to (`UV_PYTHON_INSTALL_DIR`). |
| `~/.memsearch/models/` | Hugging Face cache (`HF_HOME`) — where the ONNX embedding model lands. |
| `~/.memsearch/index.log` | Output of every background indexer the hooks spawn (model download, indexing errors). Truncated past 1 MB. |

If you already export `UV_CACHE_DIR`, `HF_HOME` or `UV_PROJECT_ENVIRONMENT`, your values are kept and
the plugin uses those instead.

### Journal format

```markdown
## Session 14:32

### 14:32
<!-- session:b1f0… turn:9f2c… transcript:/home/you/.claude/projects/…/b1f0….jsonl -->
- User asked how the retry budget interacts with the circuit breaker
- Claude Code read src/http/retry.py and found the budget is per-host, not per-request
```

Codex entries carry the other anchor kind, `<!-- session:<id> rollout:<path> -->`, because a Codex
rollout has no per-turn uuid. The `## Session HH:MM` heading is written lazily — only with the first
entry of a session that actually produced content — and the anchor itself doubles as the "heading
already written" marker. The anchors are what makes the third recall layer possible: they point back
at the raw transcript.

### Hooks

| Event | Timeout | What happens |
|---|---|---|
| `SessionStart` | 10 s | Prints `[memsearch v<version>] embedding: <provider>/<model> \| index: N chunks, updated … \| memory: <dir>`, injects a `# Recent Memory` block (two newest journals, at most 40 lines and 1800 bytes) as `additionalContext`, and spawns a detached reindex when the index is absent or stale. |
| `UserPromptSubmit` | 5 s | Pure bash, never starts Python: prints the hint `[memsearch] Recall available if needed`. |
| `Stop` | 120 s async (Claude), 30 s (Codex) | Summarizes the last turn, appends it to today's journal, then spawns a detached `index --skip-if-locked`. |

There is no SessionEnd hook — nothing needs stopping. On Codex the Stop hook is two-phase: it parses
the rollout synchronously (Codex may delete it on return), writes a work file, prints `{}`, and lets
a detached `hook stop-worker` do the summarizing and indexing.

Every hook is a no-op when `MEMSEARCH_DISABLE=1`, which is exactly how the summarizer child avoids
re-entering the hooks that spawned it.

### One memory for many projects

`MEMSEARCH_DIR` overrides the *project* directory `<git root>/.memsearch`. Export it and every
project writes to the same journals and shares one index. (`MEMSEARCH_HOME` is the other one: it
moves the runtime and caches, not your memories.)

## Configuration

`~/.memsearch/config.toml` is the whole configuration surface — `$MEMSEARCH_HOME` moves the whole
directory, `MEMSEARCH_CONFIG` moves just this file. The first SessionStart writes
`provider = "onnx"` if the file does not exist.

```toml
[embedding]
provider = "onnx"     # onnx | openai | google | voyage | jina | mistral | ollama | local
model = ""            # "" means the provider's default model
api_key = ""          # optional literal; a real environment variable always wins
base_url = ""         # openai-compatible endpoints only
batch_size = 0        # 0 means the provider's own default

[chunking]
max_chunk_size = 1500 # characters; larger sections are split at paragraph boundaries
overlap_lines = 2     # lines of context carried into a split chunk
min_chunk_size = 0    # > 0 merges consecutive small sections (one turn each) up to this size; re-index with --force after changing

[claude]
summarize_enabled = true    # false disables turn capture for Claude Code
summarize_model = "haiku"   # passed to `claude -p --model`

[codex]
summarize_enabled = true
summarize_model = "gpt-5.1-codex-mini"   # passed to `codex exec -m`

[prompts]
summarize = ""        # path to a custom template; {{AGENT_NAME}} is substituted

[memory]
filename_suffix = ""  # "hostname" writes YYYY-MM-DD-<host>.md, for a memory folder synced between machines
```

Read and write it with the CLI, always through the launcher in the plugin directory (for a
marketplace install that is under `~/.claude/plugins/marketplaces/edgarrc/`; for Codex it is
`<checkout>/bin/memsearch`):

```bash
bin/memsearch config list
bin/memsearch config get embedding.provider
bin/memsearch config set embedding.provider openai
bin/memsearch config set claude.summarize_model sonnet
```

Unknown keys are rejected; `int` and `bool` values are coerced and validated on the way in.

## Embedding providers

| Provider | API key | Notes |
|---|---|---|
| `onnx` *(default)* | — | Local `gpahal/bge-m3-onnx-int8` on onnxruntime, CPU. Downloaded once into the Hugging Face cache. |
| `openai` | `OPENAI_API_KEY` | `text-embedding-3-small`. Honours `embedding.base_url` (or `OPENAI_BASE_URL`) for compatible endpoints. |
| `google` | `GOOGLE_API_KEY` | `gemini-embedding-001`. Set `GOOGLE_GENAI_USE_VERTEXAI=true` to authenticate through Vertex AI instead. |
| `voyage` | `VOYAGE_API_KEY` | `voyage-3-lite`. |
| `jina` | `JINA_API_KEY` | `jina-embeddings-v4`. |
| `mistral` | `MISTRAL_API_KEY` | `mistral-embed`. |
| `ollama` | — | `nomic-embed-text` against a local server; address from `OLLAMA_HOST` (default `http://localhost:11434`). Dimension is auto-detected. |
| `local` | — | sentence-transformers `all-MiniLM-L6-v2`; CUDA, MPS or CPU, auto-detected. |

`embedding.api_key` is an alternative to exporting the variable; a real environment variable takes
precedence over it.

Each provider needs its own SDK, so `bin/memsearch` reads `embedding.provider` straight out of the
config file and passes `--extra onnx --extra <provider>` to `uv`. The runtime remembers which extras
it was built with, so switching provider triggers an automatic resync: the next session shows
`installing runtime in the background` and picks it up from there, or run `bin/memsearch --sync` to
do it right away (any direct `bin/memsearch <command>` call also resyncs inline first).

Switching provider or model changes the vectors, so the identity recorded in the index no longer
matches: the next `index` run empties the database and rebuilds it (and says so on stderr), while
`search`, `expand` and `stats` refuse with an actionable error until that happens.

## CLI reference

Always invoke it as `<plugin>/bin/memsearch`, never as a bare `memsearch` — the launcher is what
pins the CLI to this checkout.

| Command | What it does |
|---|---|
| `memsearch --version` | Version of the installed package. |
| `memsearch index [PATHS]... [--force] [--skip-if-locked]` | Index markdown; defaults to the project's memory directory. `--force` re-embeds everything, `--skip-if-locked` returns at once when another indexer is running. |
| `memsearch search QUERY [-k N] [--json]` | Hybrid search: dense cosine + FTS5 keywords, fused with RRF. |
| `memsearch expand CHUNK_ID [--lines N] [--json]` | The full markdown section a chunk came from. |
| `memsearch transcript PATH [--turn UUID] [--context N] [--json]` | Render the original conversation — including tool calls — from a Claude Code JSONL or a Codex rollout. |
| `memsearch config get KEY` · `set KEY VALUE` · `list [--json]` | Read and write `config.toml`. |
| `memsearch stats` | Chunk count, provider, model and last index time. |
| `memsearch reset --yes` | Empty this project's index. The markdown is untouched. |
| `memsearch hook …` | Internal: what the launchers exec. Always prints one JSON object and exits 0. |

Exit codes: `0` success, `1` runtime error, `2` usage error, `3` unrecognized transcript format.

## Troubleshooting

- **Anything unexplained: read the sync log**, `~/.memsearch/venvs/<hash>.log`. Every sync appends a
  timestamped header there, and the log is truncated when it passes 1 MB.
- **`installing runtime in the background` on every session** — the sync keeps failing. Read that
  log, then run `bin/memsearch --sync` by hand to see the error in the foreground.
- **`index: not built yet — building in background` on every session** — the background indexer
  keeps failing (no network for the model download, a broken provider, a missing API key). Read
  `~/.memsearch/index.log`, then run `bin/memsearch index` by hand to see the error in the foreground.
- **`uv not found on PATH — memory disabled`** — install uv from <https://docs.astral.sh/uv/>. The
  hooks add `~/.local/bin`, `~/bin`, `/usr/local/bin` and `/opt/homebrew/bin` before giving up.
- **`ERROR: <VAR> not set — memory search disabled`** — export the provider's key, or go back to the
  local provider with `bin/memsearch config set embedding.provider onnx`.
- **`ERROR: onnxruntime not installed`** — the runtime is incomplete: `bin/memsearch --sync`.
- **Results look stale or wrong** — `bin/memsearch index --force` re-embeds everything. To start
  clean, `bin/memsearch reset --yes` and index again; deleting `index.db` works too.
- **Turn it off** — `MEMSEARCH_DISABLE=1` makes every launcher and hook print `{}` and exit.
- **Relocate everything** — `MEMSEARCH_HOME=/somewhere/else` moves the whole directory;
  `UV_PROJECT_ENVIRONMENT` moves just the runtime environment (its log is always
  `<environment path>.log`). Useful when `$HOME` is on a filesystem you would rather keep small.
- **`this SQLite build has no FTS5 module`** — set `MEMSEARCH_NO_FTS=1` for dense-vector search only,
  or use a Python whose `sqlite3` was built with FTS5.
- **A journal is being skipped** — files above `MEMSEARCH_MAX_FILE_MB` (default 8) are reported as
  failures and skipped; the rest of the run still succeeds. Raise the limit or split the file.
- **Codex summaries look truncated** — the rollout text handed to the summarizer is capped at
  `MEMSEARCH_SUMMARY_MAX_CHARS` characters (default 8000).

## Uninstall

Removing this plugin removes everything it ever created — that is a deliberate difference from
upstream, whose self-installed `uv`, warmed package cache and globally cached embedding model all
survived an uninstall.

### What the plugin writes, and where

| Location | What | Removed by |
|---|---|---|
| The plugin checkout | **Nothing.** It is read-only at runtime. | Deleting the clone / `/plugin uninstall` |
| `~/.memsearch/` | Runtime environment, uv cache, downloaded interpreter, embedding model, `config.toml` | `uninstall.sh --purge`, or `rm -rf ~/.memsearch` |
| `<project>/.memsearch/memory/*.md` | Your journals — **kept**, they are the source of truth | You, by hand |
| `<project>/.memsearch/index.db`, `index.lock` | Derived index; safe to delete any time | `rm -f <project>/.memsearch/index.db* <project>/.memsearch/index.lock` |
| `~/.codex/hooks.json`, `~/.agents/skills/memory-recall` | Codex wiring | `uninstall.sh` |
| `$TMPDIR/memsearch-stop.*.json` | Transient Codex work files, deleted by the worker that reads them | Itself |

### Claude Code

```
/plugin uninstall memsearch-mini@edgarrc
/plugin marketplace remove edgarrc
```

### Codex

```bash
bash /path/to/memsearch-mini/uninstall.sh
```

It removes only memsearch's own entries from `~/.codex/hooks.json`, and removes
`~/.agents/skills/memory-recall` only if the skill there is actually memsearch's. It deliberately
leaves `hooks = true` under `[features]` in `~/.codex/config.toml` alone, because other tools may
depend on it. It also prints how much `~/.memsearch` is holding, and the two Claude Code commands
above.

### Then, for either host

```bash
bash /path/to/memsearch-mini/uninstall.sh --purge   # or simply: rm -rf ~/.memsearch
```

That takes the runtime, the uv cache, any interpreter uv downloaded, the embedding model and your
config with it. Project journals are untouched. Finally, delete the checkout if you cloned one.

## Migrating from upstream memsearch

Nothing here runs automatically — these are the steps to run on your own machine.

**Claude Code**

```
/plugin uninstall memsearch
/plugin marketplace remove memsearch-plugins
/plugin marketplace add edgarrc/memsearch-mini
/plugin install memsearch-mini@edgarrc
```

**Codex** — delete the skills that no longer exist, then reinstall:

```bash
rm -rf ~/.agents/skills/memory-config ~/.agents/skills/memory-to-skill
bash /path/to/memsearch-mini/codex/install.sh
```

The installer replaces `~/.agents/skills/memory-recall` and strips upstream's hook entries
(`plugins/codex/hooks/…`) from `~/.codex/hooks.json`, keeping a `.bak` of the previous file.

**Optional cleanup of the Milvus era** — none of this is read any more:

```bash
rm -f ~/.memsearch/milvus.db*  ~/.memsearch/.pypi-latest
rm -f .memsearch/.index-state.json .memsearch/.watch.pid .memsearch/.index.pid   # per project
pkill -f milvus_lite            # leftover background processes, if any
uv tool uninstall memsearch     # the old standalone CLI, if you installed it
```

Upstream also left the embedding model in the shared Hugging Face cache (usually
`~/.cache/huggingface/hub/models--gpahal--bge-m3-onnx-int8`) and the PyPI package in uv's cache.
This fork redownloads the model into `~/.memsearch/models/`, so the old copy is only worth keeping if
another tool of yours uses that cache.

**Config** — keep `[embedding]` and `[chunking]` as they are. `[milvus]`, `[llm]`, `[reranker]` and
`[plugins.*]` are no longer read (they are ignored, so you can leave them or delete them), and
project-level `.memsearch.toml` files are gone: `~/.memsearch/config.toml` is the only layer.
Summarization is configured with `[claude]` / `[codex]` `summarize_enabled` and `summarize_model`
instead of the old `[plugins.<agent>.summarize]` block. `prompts.summarize` still works.

**Your journals carry over unchanged.** `.memsearch/memory/*.md` is the same format; the first
session after installing sees no index, builds one in the background, and everything is searchable
again. Chunk ids changed, but nothing on disk refers to them.

## Development

```bash
uv sync --extra onnx --group dev
uv run python -m pytest
uv run ruff check src tests
uv run ruff format --check src tests
```

CI (`.github/workflows/test.yml`) runs the same commands on Python 3.10 and 3.12, from the lockfile,
which is the same path a user's install takes. A plain `uv sync` like the one above puts a `.venv` in
the checkout, which is fine for development — the installed plugin never does that, it keeps its
environment under `~/.memsearch/venvs/`.

**Release.** Bump the version in three files — `pyproject.toml`, `.claude-plugin/plugin.json`, and
`.claude-plugin/marketplace.json` (both `metadata.version` and the plugin entry). `tests/test_packaging.py`
fails if they disagree. Then `uv lock`, run the tests, commit, and tag `vX.Y.Z`. Users pick the new
version up with `/plugin update`.

## License

MIT — see [LICENSE](LICENSE). This is a derivative work of
[zilliztech/memsearch](https://github.com/zilliztech/memsearch), Copyright (c) 2025 Zilliz Inc.,
distributed under the same license.
