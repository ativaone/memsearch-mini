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
>   stdin to Python; all logic lives in `src/memsearch_mini/hooks.py` and `capture.py`.
> - **The CLI runs from this checkout via `uv run`**, not from a package installed from PyPI, so the
>   hooks and the CLI they call can never come from different installs.
> - **Uninstalling leaves nothing behind.** Upstream's hooks installed `uv` for you with
>   `curl -LsSf https://astral.sh/uv/install.sh | sh`, ran the CLI as
>   `uvx --from "memsearch[onnx]" memsearch` (which warms uv's cache with the PyPI package), and let
>   the ONNX model download into the global Hugging Face cache. Uninstalling the plugin removed none
>   of that.
> - This fork installs no tools on your behalf — `uv` is a documented prerequisite — and points the
>   runtime, the uv cache, any downloaded interpreter and the model cache at `~/.memsearch-mini`.
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
  `.memsearch-mini/memory/YYYY-MM-DD.md`.
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
  hundred MB) into `~/.memsearch-mini/models/`, plus roughly 90 MB of wheels in the runtime environment
  under `~/.memsearch-mini/venvs/`.
- **POSIX** (Linux, macOS, WSL): the hooks rely on `select` on a pipe and detached process groups.

## Install — Claude Code

```
/plugin marketplace add ativaone/memsearch-mini
/plugin install memsearch-mini@ativaone
```

Installing registers the hooks but does not build the runtime. Quit Claude Code and run, from the
project directory:

```bash
~/.claude/plugins/cache/ativaone/memsearch-mini/*/bin/memsearch-mini index
```

It installs the runtime under `~/.memsearch-mini/venvs/`, downloads the embedding model (`onnx`, the
default) or checks the API key (cloud providers), and creates the empty index, all in the foreground.
The `*` resolves to the installed version — the copy the hooks themselves run from, printed as
`installPath` by `claude plugin list --json`. Runtimes are keyed by plugin path, so don't use the
launcher in the clone under `~/.claude/plugins/marketplaces/`: it builds a second runtime the hooks
never use, and the next session still installs the real one in the background.
Want a provider other than `onnx`? Write `~/.memsearch-mini/config.toml` before this step (see
[Configuration](#configuration)). The command resolves `.memsearch-mini` from the git root of the
current directory, so run it inside the project. Open Claude Code again: the first line is

```
[memsearch-mini v…] embedding: onnx/gpahal/bge-m3-onnx-int8 | index: 0 chunks … | memory: <dir>
```

and every turn from then on is captured.

Skipped the step? The plugin bootstraps itself. The next SessionStart prints
`[memsearch-mini] installing runtime in the background — memory available from the next session`, a
detached `uv sync` builds the runtime (logging to the `.log` file beside it) and then runs the
SessionStart hook once, so the model download and the first index start right away. Until the
runtime is ready, the Stop and UserPromptSubmit hooks print `{}` and do nothing — that session is
not captured, on purpose, because a 90 MB download does not belong inside a hook timeout. Under a
sandbox that kills background children, prefer the foreground step; see [Sandbox](#sandbox).

For local development, point Claude Code at a working tree instead of the marketplace:

```bash
claude --plugin-dir /path/to/memsearch-mini
```

*Running Claude Code under a sandbox (the `sandbox` block of `settings.json`, a bubblewrap wrapper)?
See [Sandbox](#sandbox) before the first session.*

## Install — Codex

```bash
git clone https://github.com/ativaone/memsearch-mini.git
bash memsearch-mini/codex/install.sh
```

The installer runs five steps: it checks for `uv`; runs a blocking `bin/memsearch-mini --sync`; copies
`codex/skills/memory-recall` to `~/.agents/skills/memory-recall`; backs up `~/.codex/hooks.json` to
`~/.codex/hooks.json.bak` and merges in three entries (SessionStart 10 s, UserPromptSubmit 5 s,
Stop 30 s); sets `hooks = true` under `[features]` in `~/.codex/config.toml`; and marks the scripts
executable. Set `MEMSEARCH_MINI_SKIP_SYNC=1` to skip the blocking sync and let the first session do it.

The checkout path is baked into both `~/.codex/hooks.json` and the installed skill, so **re-run the
installer after moving or renaming the clone**. It is idempotent: it strips its own old entries
(matching any `/hooks/<script>`) as well as upstream's (`plugins/codex/hooks/<script>`) before
writing, and leaves unrelated hooks alone.

Codex sandboxing: the first index needs network access to download the embedding model. Upstream's
README recommended running that first session with full access
(`codex --dangerously-bypass-approvals-and-sandbox`); after the model is cached, a read-only sandbox
is enough. An outer wrapper around the host (bubblewrap) has its own rules: see [Sandbox](#sandbox).

To undo all of this, see [Uninstall](#uninstall).

## How it works

```
turn ends ─▶ Stop hook ─▶ parse last turn ─▶ claude -p / codex exec ─▶ bullets
                                                                        │
              .memsearch-mini/memory/YYYY-MM-DD.md  ◀──────────────────┘
                            │
              index (detached) ─▶ chunk ─▶ embed ─▶ .memsearch-mini/index.db
                                                          │
  question ─▶ memory-recall skill ─▶ search (dense + FTS5, fused with RRF)
                                       └─▶ expand ─▶ transcript
```

### On disk

Two places, and only two: the project, and `$MEMSEARCH_MINI_HOME` (default `~/.memsearch-mini`). **The plugin
checkout is never written to at runtime** — no virtualenv inside it, no caches, no state.

Per project:

| Path | What it is |
|---|---|
| `<project>/.memsearch-mini/memory/YYYY-MM-DD.md` | Daily journal — **the source of truth**. Plain markdown, editable, versionable. |
| `<project>/.memsearch-mini/index.db` | Derived SQLite index: chunk rows, float32 embeddings, an FTS5 table. Rebuildable. |
| `<project>/.memsearch-mini/index.lock` | Held by a running indexer; `index --skip-if-locked` gives up instead of queueing. |
| `<project>/.memsearch-mini/pending/` | One small JSON per turn whose summary is in flight (transcript path, turn uuid, time — never content). Empty whenever every turn has been written; see [Hooks](#hooks). |

Under `$MEMSEARCH_MINI_HOME`:

| Path | What it is |
|---|---|
| `~/.memsearch-mini/config.toml` | The only configuration layer. |
| `~/.memsearch-mini/venvs/<hash>/` | The runtime, created and kept current by `uv` (`UV_PROJECT_ENVIRONMENT`). The hash comes from the plugin path, so a Claude Code install and a Codex clone get one each. |
| `~/.memsearch-mini/venvs/<hash>.log` · `<hash>.lock` | Sync log and sync lock — *siblings* of the environment, never inside it. |
| `~/.memsearch-mini/uv-cache/` | uv's package cache (`UV_CACHE_DIR`). |
| `~/.memsearch-mini/python/` | Interpreter uv downloaded, if it had to (`UV_PYTHON_INSTALL_DIR`). |
| `~/.memsearch-mini/models/` | Hugging Face cache (`HF_HOME`) — where the ONNX embedding model lands. |
| `~/.memsearch-mini/index.log` | Output of every background indexer the hooks spawn (model download, indexing errors). Truncated past 1 MB. |

Environments are keyed by the plugin path, and Claude Code installs every version under a directory
of its own, so an update builds a new environment and abandons the previous one. `<hash>.root` is
what makes that recoverable: it records the checkout the environment belongs to, and once that path
is gone — Claude Code keeps the old version directory for about two weeks — the next SessionStart
deletes the environment and its sidecars. Environments built before this existed carry no `.root`
and are never collected; `rm -rf ~/.memsearch-mini/venvs` is always safe, the runtime rebuilds.

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
| `SessionStart` | 10 s | Prints `[memsearch-mini v<version>] embedding: <provider>/<model> \| index: N chunks, updated … \| memory: <dir>`, injects a `# Recent Memory` block (two newest journals, at most 40 lines and 1800 bytes) as `additionalContext`, and spawns a detached reindex when the index is absent or stale. |
| `UserPromptSubmit` | 5 s | Pure bash, never starts Python: prints the hint `[memsearch-mini] Recall available if needed`. |
| `Stop` | 120 s async (Claude), 30 s (Codex) | Records the turn under `.memsearch-mini/pending/`, summarizes it, appends it to today's journal, drops the record, then spawns a detached `index --skip-if-locked`. On Claude it ends with a `systemMessage` — `[memsearch-mini] turn captured`, or `turn recorded without a summary (<reason>)` — which the host shows when the async hook completes: the quiet sign that it is safe to quit. |

There is no SessionEnd hook — nothing needs stopping. On Codex the Stop hook is two-phase: it parses
the rollout synchronously (Codex may delete it on return), writes a work file, prints `{}`, and lets
a detached `hook stop-worker` do the summarizing and indexing.

A Claude Stop hook that dies mid-summary — the host quit, a sandbox wrapper killed the process tree —
leaves its record behind. A record older than 150 s (a hook cannot outlive its 120 s timeout) is
handed by the next `SessionStart` or `Stop` in that project to a detached `hook recover`, which
re-reads that exact turn from the transcript (by uuid, so a resumed session does not confuse it),
writes it into the journal of the day it happened, skips it if it is already there, and reindexes.
The hook that spawned the worker says so in its status (`recovering N earlier turn(s) in the
background`); a record too young to touch is reported as `N turn(s) pending, recovery at the next
turn end`. Codex keeps its own two-phase scheme and does not use the pending directory.

Every hook is a no-op when `MEMSEARCH_MINI_DISABLE=1`, which is exactly how the summarizer child avoids
re-entering the hooks that spawned it.

### One memory for many projects

`MEMSEARCH_MINI_DIR` overrides the *project* directory `<git root>/.memsearch-mini`. Export it and every
project writes to the same journals and shares one index. (`MEMSEARCH_MINI_HOME` is the other one: it
moves the runtime and caches, not your memories.)

## Configuration

`~/.memsearch-mini/config.toml` is the whole configuration surface — `$MEMSEARCH_MINI_HOME` moves the whole
directory, `MEMSEARCH_MINI_CONFIG` moves just this file. The first SessionStart writes
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
marketplace install that is under `~/.claude/plugins/cache/ativaone/memsearch-mini/<version>/`; for
Codex it is `<checkout>/bin/memsearch-mini`):

```bash
bin/memsearch-mini config list
bin/memsearch-mini config get embedding.provider
bin/memsearch-mini config set embedding.provider openai
bin/memsearch-mini config set claude.summarize_model sonnet
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

Each provider needs its own SDK, so `bin/memsearch-mini` reads `embedding.provider` straight out of the
config file and passes `--extra onnx --extra <provider>` to `uv`. The runtime remembers which extras
it was built with, so switching provider triggers an automatic resync: the next session shows
`installing runtime in the background` and picks it up from there, or run `bin/memsearch-mini --sync` to
do it right away (any direct `bin/memsearch-mini <command>` call also resyncs inline first).

Switching provider or model changes the vectors, so the identity recorded in the index no longer
matches: the next `index` run empties the database and rebuilds it (and says so on stderr), while
`search`, `expand` and `stats` refuse with an actionable error until that happens.

## CLI reference

Always invoke it as `<plugin>/bin/memsearch-mini`, never as a bare `memsearch-mini` — the launcher is what
pins the CLI to this checkout.

| Command | What it does |
|---|---|
| `memsearch-mini --version` | Version of the installed package. |
| `memsearch-mini index [PATHS]... [--force] [--skip-if-locked]` | Index markdown; defaults to the project's memory directory. `--force` re-embeds everything, `--skip-if-locked` returns at once when another indexer is running. |
| `memsearch-mini search QUERY [-k N] [--json]` | Hybrid search: dense cosine + FTS5 keywords, fused with RRF. |
| `memsearch-mini expand CHUNK_ID [--lines N] [--json]` | The full markdown section a chunk came from. |
| `memsearch-mini transcript PATH [--turn UUID] [--context N] [--json]` | Render the original conversation — including tool calls — from a Claude Code JSONL or a Codex rollout. |
| `memsearch-mini config get KEY` · `set KEY VALUE` · `list [--json]` | Read and write `config.toml`. |
| `memsearch-mini stats` | Chunk count, provider, model and last index time. |
| `memsearch-mini reset --yes` | Empty this project's index. The markdown is untouched. |
| `memsearch-mini hook …` | Internal: what the launchers exec. Always prints one JSON object and exits 0. |

Exit codes: `0` success, `1` runtime error, `2` usage error, `3` unrecognized transcript format.

## Troubleshooting

- **Anything unexplained: read the sync log**, `~/.memsearch-mini/venvs/<hash>.log`. Every sync appends a
  timestamped header there, and the log is truncated when it passes 1 MB.
- **`installing runtime in the background` on every session** — the sync keeps failing. Read that
  log, then run `bin/memsearch-mini --sync` by hand to see the error in the foreground.
- **`index: not built yet — building in background` on every session** — the background indexer
  keeps failing (no network for the model download, a broken provider, a missing API key). Read
  `~/.memsearch-mini/index.log`, then run `bin/memsearch-mini index` by hand to see the error in the foreground.
- **`uv not found on PATH — memory disabled`** — install uv from <https://docs.astral.sh/uv/>. The
  hooks add `~/.local/bin`, `~/bin`, `/usr/local/bin` and `/opt/homebrew/bin` before giving up.
- **`ERROR: <VAR> not set — memory search disabled`** — export the provider's key, or go back to the
  local provider with `bin/memsearch-mini config set embedding.provider onnx`.
- **`ERROR: onnxruntime not installed`** — the runtime is incomplete: `bin/memsearch-mini --sync`.
- **Results look stale or wrong** — `bin/memsearch-mini index --force` re-embeds everything. To start
  clean, `bin/memsearch-mini reset --yes` and index again; deleting `index.db` works too.
- **Turn it off** — `MEMSEARCH_MINI_DISABLE=1` makes every launcher and hook print `{}` and exit.
- **Relocate everything** — `MEMSEARCH_MINI_HOME=/somewhere/else` moves the whole directory;
  `UV_PROJECT_ENVIRONMENT` moves just the runtime environment (its log is always
  `<environment path>.log`). Useful when `$HOME` is on a filesystem you would rather keep small.
- **`this SQLite build has no FTS5 module`** — set `MEMSEARCH_MINI_NO_FTS=1` for dense-vector search only,
  or use a Python whose `sqlite3` was built with FTS5.
- **A journal is being skipped** — files above `MEMSEARCH_MINI_MAX_FILE_MB` (default 8) are reported as
  failures and skipped; the rest of the run still succeeds. Raise the limit or split the file.
- **Codex summaries look truncated** — the rollout text handed to the summarizer is capped at
  `MEMSEARCH_MINI_SUMMARY_MAX_CHARS` characters (default 8000).
- **A turn is missing from a journal** — look in `<project>/.memsearch-mini/pending/`. A record there means
  its Stop hook died before writing; it is recovered at the next session start or turn end, once it
  is 150 s old. A `.working` suffix means the recovery is running right now.

## Sandbox

Claude Code can run under two sandboxes at once: the harness's own `sandbox` block in `settings.json`
(a write allowlist and a network filter that apply **only to the Bash tool**) and an outer wrapper
such as bubblewrap that mounts everything read-only except a few paths. The plugin works under both
once each layer knows about `$MEMSEARCH_MINI_HOME`.

### Which layer runs what

| Code path | Runs | Writes | Network |
|---|---|---|---|
| Hooks and the children they detach (`uv sync`, the indexer, `claude -p`) | outside the Bash sandbox, inside the wrapper | `~/.memsearch-mini`, `<project>/.memsearch-mini`, `~/.claude` (the summarizer's own state) | `claude -p` reaches the API; the first index downloads the model from `huggingface.co` |
| `memory-recall` skill (`bin/memsearch-mini search` / `expand` / `transcript`) | through the Bash tool, so inside the Bash sandbox | `~/.memsearch-mini/uv-cache` — `uv run` refuses to start when its cache is read-only, even with `--frozen --no-sync` | none once the model is cached |
| The plugin checkout | — | nothing, ever | — |

### settings.json

```json
"permissions": { "deny": ["Edit(~/.memsearch-mini/**)"] },
"sandbox": { "filesystem": { "allowWrite": ["~/.memsearch-mini/"] } }
```

Write access, not just read — reading is allowed by default anyway. The `Edit` deny is optional
defence in depth: nothing under `~/.memsearch-mini` is meant to be hand-edited (`bin/memsearch-mini config set`
covers the config). No extra network domain is needed: the model download runs in a hook child,
outside the Bash filter. Allow `huggingface.co` and `*.hf.co` only if you want to run
`bin/memsearch-mini --sync` or `index` from the Bash tool before the background indexer has cached the
model.

### An outer wrapper (bubblewrap and friends)

Bind `~/.memsearch-mini` writable and create it before the wrapper starts: a bind to a missing directory
fails or is skipped, and `~` is read-only inside. `~/.claude` has to be writable for the harness
itself, which already covers the plugin checkout under `~/.claude/plugins` (never written to) and
the summarizer's transcripts.

Wrappers usually add `--unshare-pid` and `--die-with-parent`. Together they mean **no process outlives
the session**: when `claude` exits, the PID namespace is torn down and every detached child dies with
it — exactly the children this plugin relies on. It is built to survive that:

- **First install.** Follow the foreground step in [Install — Claude Code](#install--claude-code)
  (for `--plugin-dir` or Codex the launcher is `/path/to/memsearch-mini/bin/memsearch-mini index`):
  nothing is left running in the background. If you skipped it, the detached `uv sync` (≈90 MB),
  the model download (a few hundred MB) and the first index all run in the background of the next
  session. Quit before they finish and the sync lock (`~/.memsearch-mini/venvs/<hash>.lock`) is left
  behind: sessions in the next 30 minutes give up at once and print `installing runtime in the
  background` again, then the lock goes stale and the next session retries. The model download
  resumes from its `.incomplete` files.

- **Indexer killed mid-run.** The next `SessionStart` sees journals newer than the index and
  reindexes.
- **Stop hook killed mid-summary.** The hook records the turn under `<project>/.memsearch-mini/pending/`
  before it starts `claude -p`; the next `SessionStart` or `Stop` in that project recovers it (see
  [Hooks](#hooks)). The summary lands in the journal of the day the turn happened, so quitting right
  after an answer costs nothing but the delay. Wait for `[memsearch-mini] turn captured` if the last
  turn matters; `pending/` is empty when every turn has been written, so a status line can watch it
  if you want a permanent "still writing" marker.

## Uninstall

Removing this plugin removes everything it ever created — that is a deliberate difference from
upstream, whose self-installed `uv`, warmed package cache and globally cached embedding model all
survived an uninstall.

### What the plugin writes, and where

| Location | What | Removed by |
|---|---|---|
| The plugin checkout | **Nothing.** It is read-only at runtime. | Deleting the clone / `/plugin uninstall` |
| `~/.memsearch-mini/` | Runtime environment, uv cache, downloaded interpreter, embedding model, `config.toml` | `uninstall.sh --purge`, or `rm -rf ~/.memsearch-mini` |
| `<project>/.memsearch-mini/memory/*.md` | Your journals — **kept**, they are the source of truth | You, by hand |
| `<project>/.memsearch-mini/index.db`, `index.lock` | Derived index; safe to delete any time | `rm -f <project>/.memsearch-mini/index.db* <project>/.memsearch-mini/index.lock` |
| `<project>/.memsearch-mini/pending/` | Records of turns still being summarized; empty in a healthy install | Itself, or `rm -rf` |
| `~/.codex/hooks.json`, `~/.agents/skills/memory-recall` | Codex wiring | `uninstall.sh` |
| `$TMPDIR/memsearch-mini-stop.*.json` | Transient Codex work files, deleted by the worker that reads them | Itself |

### Claude Code

```
/plugin uninstall memsearch-mini@ativaone
/plugin marketplace remove ativaone
```

### Codex

```bash
bash /path/to/memsearch-mini/uninstall.sh
```

It removes only memsearch-mini's own entries from `~/.codex/hooks.json`, and removes
`~/.agents/skills/memory-recall` only if the skill there is actually memsearch-mini's. It deliberately
leaves `hooks = true` under `[features]` in `~/.codex/config.toml` alone, because other tools may
depend on it. It also prints how much `~/.memsearch-mini` is holding, and the two Claude Code commands
above.

### Then, for either host

```bash
bash /path/to/memsearch-mini/uninstall.sh --purge   # or simply: rm -rf ~/.memsearch-mini
```

That takes the runtime, the uv cache, any interpreter uv downloaded, the embedding model and your
config with it. Project journals are untouched. Finally, delete the checkout if you cloned one.

## Migrating from upstream memsearch

Nothing here runs automatically — these are the steps to run on your own machine.

**Claude Code**

```
/plugin uninstall memsearch
/plugin marketplace remove memsearch-plugins
/plugin marketplace add ativaone/memsearch-mini
/plugin install memsearch-mini@ativaone
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
This fork redownloads the model into `~/.memsearch-mini/models/`, so the old copy is only worth keeping if
another tool of yours uses that cache.

**Config** — keep `[embedding]` and `[chunking]` as they are. `[milvus]`, `[llm]`, `[reranker]` and
`[plugins.*]` are no longer read (they are ignored, so you can leave them or delete them), and
project-level `.memsearch.toml` files are gone: `~/.memsearch-mini/config.toml` is the only layer.
Summarization is configured with `[claude]` / `[codex]` `summarize_enabled` and `summarize_model`
instead of the old `[plugins.<agent>.summarize]` block. `prompts.summarize` still works.

**Your journals move with you.** The format is unchanged, but the directory is not — create
`.memsearch-mini/` in each project and move the markdown across:

```bash
mkdir -p .memsearch-mini
mv .memsearch/memory .memsearch-mini/memory                  # per project
mv ~/.memsearch/config.toml ~/.memsearch-mini/config.toml    # optional, keeps your settings
```

The first session after that sees no index and builds one from the markdown in the background —
SQLite only, nothing is read from Milvus — and everything is searchable again. Chunk ids changed,
but nothing on disk refers to them.

## Upstream sync

Keeping up with [zilliztech/memsearch](https://github.com/zilliztech/memsearch) is part of this
repository's routine, not an afterthought — but it is done case by case, never as a merge. Every
upstream issue and pull request is judged against this fork's reduced scope: does the concept
apply here, and can it be fixed without adding the machinery this fork exists to avoid? What
applies is ported in this codebase's own terms, with tests. What does not is recorded with the
reason.

- **Everything up to 2026-09-10 has been reviewed** — 248 upstream issues and pull requests,
  including the whole open backlog at that date.
- **Future items are tracked as issues in this repository.** Each port opens an issue here that
  links the upstream item, states why it applies and what was done, and is closed by the commit
  that references it. Not every upstream item will apply — most will not — but every one of them
  is analyzed, and the verdicts live in `.claude/upstream-sync/entries.jsonl`.
- The routine is the project-local Claude Code skill `upstream-sync` (`.claude/skills/upstream-sync/`),
  which lists what is still unjudged, records verdicts, and prints a console report of each run.

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
environment under `~/.memsearch-mini/venvs/`.

**Release.** Bump the version in three files — `pyproject.toml`, `.claude-plugin/plugin.json`, and
`.claude-plugin/marketplace.json` (both `metadata.version` and the plugin entry). `tests/test_packaging.py`
fails if they disagree. Then `uv lock`, run the tests, commit, and tag `vX.Y.Z`. Users pick the new
version up with `/plugin update`.

## License

MIT — see [LICENSE](LICENSE). This is a derivative work of
[zilliztech/memsearch](https://github.com/zilliztech/memsearch), Copyright (c) 2025 Zilliz Inc.,
distributed under the same license.
