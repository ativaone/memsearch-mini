#!/usr/bin/env bash
# Installer for the memsearch-mini Codex CLI plugin.
#
#   bash codex/install.sh
#
# Copies the memory-recall skill into ~/.agents/skills, merges the memsearch-mini
# hook entries into ~/.codex/hooks.json and enables the hooks feature flag.
# Set MEMSEARCH_MINI_SKIP_SYNC=1 to skip the blocking runtime sync.
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
CODEX_DIR="$HOME/.codex"
HOOKS_FILE="$CODEX_DIR/hooks.json"
CONFIG_FILE="$CODEX_DIR/config.toml"
SKILL_SRC="$INSTALL_DIR/codex/skills/memory-recall"
SKILL_DST="$HOME/.agents/skills/memory-recall"
MEMSEARCH_MINI_HOME="${MEMSEARCH_MINI_HOME:-$HOME/.memsearch-mini}"
# Ask the library for the environment path instead of duplicating its hashing
# rule; a subshell keeps this script's `set -e` away from the sourced file.
VENV_FROM_LIB="$(bash -c 'source "$1/hooks/common.sh"; printf "%s\n" "$VENV"' _ "$INSTALL_DIR" 2>/dev/null || true)"
VENV="$VENV_FROM_LIB"
[ -n "$VENV" ] || VENV="$MEMSEARCH_MINI_HOME/venvs"

# The `uv run` fallback in run_python must never build an environment inside the
# checkout: without UV_PROJECT_ENVIRONMENT uv creates $INSTALL_DIR/.venv. Only
# the library's answer is a real environment path — the line above is a
# display-only placeholder — so export just that one. A value the user set
# already wins: common.sh honours it and hands it back here unchanged.
if [ -n "$VENV_FROM_LIB" ]; then
  export UV_PROJECT_ENVIRONMENT="$VENV_FROM_LIB"
fi
UV_CACHE_DIR="${UV_CACHE_DIR:-$MEMSEARCH_MINI_HOME/uv-cache}"
UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$MEMSEARCH_MINI_HOME/python}"
export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR

# The JSON/TOML edits below are the only Python this installer needs. Preference
# order: a python3 that can also parse-check TOML (3.11+ tomllib, or tomli) —
# that is what arms the config.toml validity guard — then the runtime venv's
# interpreter (tomli is a project dependency there), then a bare python3 with
# the guard degraded to best effort, and only last the interpreter uv manages.
run_python() {
  if command -v python3 >/dev/null 2>&1 \
     && { python3 -c 'import tomllib' 2>/dev/null || python3 -c 'import tomli' 2>/dev/null; }; then
    python3 - "$@"
  elif [ -x "$VENV/bin/python" ]; then
    "$VENV/bin/python" - "$@"
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$@"
  else
    uv run --project "$INSTALL_DIR" --frozen --no-sync python - "$@"
  fi
}

replace_text_in_file() {
  run_python "$1" "$2" "$3" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
path.write_text(path.read_text(encoding="utf-8").replace(sys.argv[2], sys.argv[3]), encoding="utf-8")
PY
}

ensure_hooks_enabled() {
  run_python "$1" <<'PY'
from pathlib import Path
import os
import re
import shutil
import sys

try:  # 3.11+
    import tomllib
except ImportError:  # pragma: no cover - older interpreters, or no tomli
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

path = Path(sys.argv[1])


def commit(new_text):
    """Back the original up once, then swap the new text in atomically."""
    backup = path.with_name(path.name + ".bak")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_text)
    os.replace(tmp, path)


if not path.exists():
    commit("[features]\nhooks = true\n")
    raise SystemExit

text = path.read_text()

# A dotted key is checked first: TOML forbids declaring a table twice, so once
# `features.x = ...` exists at top level, appending `[features]` makes the file
# invalid. Appending a bare dotted key at EOF is just as wrong — it would land
# in whatever table happens to be last.
#
# Only the region before the first table header is top level: the same
# `features.hooks = false` under `[profiles.dev]` is that profile's flag, and
# rewriting it there would leave Codex's own flag untouched.
first_header = re.search(r"(?m)^[ \t]*\[", text)
top = text if first_header is None else text[:first_header.start()]

dotted_hooks = re.search(r"(?m)^[ \t]*features\.hooks[ \t]*=.*$", top)
# Tolerant header match: `[features]` may carry indentation and a comment.  A
# header declares the table wherever it sits, so this one searches the whole file.
features_match = None if dotted_hooks else re.search(r"(?m)^[ \t]*\[features\][ \t]*(?:#.*)?$", text)

if dotted_hooks:
    # Spliced by span — a substitution over `text` would reach into the tables below.
    text = text[:dotted_hooks.start()] + "features.hooks = true" + text[dotted_hooks.end():]
elif features_match:
    next_section = re.search(r"(?m)^[ \t]*\[[^]]+\]", text[features_match.end():])
    block_end = len(text) if next_section is None else features_match.end() + next_section.start()
    block = text[features_match.end():block_end]
    # Indentation-tolerant like the header above: an indented `hooks = false` that
    # reads as absent gets a second `hooks` key beside it, which is invalid TOML.
    block = re.sub(r"(?m)^[ \t]*codex_hooks[ \t]*=.*\n?", "", block)
    if re.search(r"(?m)^[ \t]*hooks[ \t]*=", block):
        block = re.sub(r"(?m)^[ \t]*hooks[ \t]*=.*$", "hooks = true", block)
    else:
        if block and not block.startswith("\n"):
            block = "\n" + block
        block = "\nhooks = true" + block
    text = text[:features_match.end()] + block + text[block_end:]
else:
    # `top` again: a sibling line only belongs next to a key that is top level too.
    dotted_other = list(re.finditer(r"(?m)^[ \t]*features\.[A-Za-z0-9_-]+[ \t]*=.*$", top))
    if dotted_other:
        last = dotted_other[-1]  # an offset into a prefix of `text`, so it indexes `text` as well
        text = text[:last.end()] + "\nfeatures.hooks = true" + text[last.end():]
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n[features]\nhooks = true\n"

# Last line of defence: never hand Codex a config it can no longer parse.
if tomllib is not None:
    try:
        tomllib.loads(text)
    except Exception as exc:
        sys.stderr.write(f"  ! {path} left untouched: the edit would not be valid TOML ({exc})\n")
        raise SystemExit(1)

commit(text)
PY
}

install_or_update_hooks_file() {
  run_python "$1" "$2" <<'PY'
from pathlib import Path
import json
import math
import os
import sys

hooks_file = Path(sys.argv[1])
install_dir = sys.argv[2]

spec = {
    "SessionStart": {"script": "session-start.sh", "timeout": 10},
    "UserPromptSubmit": {"script": "user-prompt-submit.sh", "timeout": 5},
    "Stop": {"script": "stop.sh", "timeout": 30},
}


def convert_legacy_array(items):
    data = {"hooks": {}}
    for item in items:
        if not isinstance(item, dict):
            continue
        event = item.get("event")
        command = item.get("command")
        if not event or not command:
            continue
        hook = {"type": "command", "command": command}
        timeout_ms = item.get("timeout_ms")
        if isinstance(timeout_ms, (int, float)):
            hook["timeout"] = max(1, math.ceil(timeout_ms / 1000))
        if item.get("async") is True:
            hook["async"] = True
        data["hooks"].setdefault(event, []).append(
            {"matcher": item.get("matcher", ""), "hooks": [hook]}
        )
    return data


def load_existing():
    if not hooks_file.exists():
        return {"hooks": {}}

    try:
        parsed = json.loads(hooks_file.read_text())
    except ValueError:
        return {"hooks": {}}
    if isinstance(parsed, list):
        return convert_legacy_array(parsed)
    if isinstance(parsed, dict) and isinstance(parsed.get("hooks"), dict):
        return parsed
    return {"hooks": {}}


def ours(command, script_name):
    """Two markers: the upstream layout, and this plugin's layout at any checkout
    path (so a moved or renamed clone leaves no duplicate behind). The second one
    is a bare `/hooks/<script>`, which any third-party tool may also match, so it
    additionally requires our trailing platform argument — every entry we have
    ever written, quoted or not, ends with " codex"."""
    if not isinstance(command, str):
        return False
    if f"plugins/codex/hooks/{script_name}" in command:
        return True
    return f"/hooks/{script_name}" in command and command.rstrip().endswith(" codex")


def strip_old_memsearch_mini(entries, script_name):
    cleaned = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hooks = []
        for hook in entry.get("hooks", []):
            command = hook.get("command", "") if isinstance(hook, dict) else ""
            if ours(command, script_name):
                continue
            hooks.append(hook)
        if hooks:
            copied = dict(entry)
            copied["hooks"] = hooks
            cleaned.append(copied)
    return cleaned


data = load_existing()
hooks = data.setdefault("hooks", {})

for event, details in spec.items():
    script = details["script"]
    cleaned = strip_old_memsearch_mini(hooks.get(event, []), script)
    cleaned.append(
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    # Quoted: the command is a shell string, and a checkout path
                    # with a space in it would otherwise be split into two words.
                    "command": f'bash "{install_dir}/hooks/{script}" codex',
                    "timeout": details["timeout"],
                }
            ],
        }
    )
    hooks[event] = cleaned

# Write via sibling temp + os.replace so an interrupted write never leaves hooks_file truncated.
tmp = hooks_file.with_name(hooks_file.name + ".tmp")
tmp.write_text(json.dumps(data, indent=2) + "\n")
os.replace(tmp, hooks_file)
PY
}

echo "=== memsearch-mini Codex CLI plugin installer ==="
echo "Checkout: $INSTALL_DIR"
echo ""

echo "[1/5] Checking uv..."
if ! command -v uv >/dev/null 2>&1; then
  echo "  ✗ uv not found. memsearch-mini runs its CLI through uv; install it first:"
  echo "    https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi
echo "  ✓ uv found: $(command -v uv)"

echo "[2/5] Syncing the runtime (first run downloads the embedding stack)..."
if [ "${MEMSEARCH_MINI_SKIP_SYNC:-}" = "1" ]; then
  echo "  ⚠ MEMSEARCH_MINI_SKIP_SYNC=1 — skipped; the first session syncs in the background"
else
  bash "$INSTALL_DIR/bin/memsearch-mini" --sync
fi

echo "[3/5] Installing the memory-recall skill..."
mkdir -p "$HOME/.agents/skills"
# Substitute on a sibling temp and swap it in: an abort mid-install can then
# never leave a half-substituted skill, and a failure here leaves whatever
# skill is already installed exactly as it was.
SKILL_TMP="$SKILL_DST.tmp.$$"
rm -rf "$SKILL_DST".tmp.*
cp -r "$SKILL_SRC" "$SKILL_TMP"
replace_text_in_file "$SKILL_TMP/SKILL.md" "__INSTALL_DIR__" "$INSTALL_DIR"
if [ -e "$SKILL_DST" ] || [ -L "$SKILL_DST" ]; then
  echo "  ⚠ Existing memory-recall skill found — replacing"
  rm -rf "$SKILL_DST"
fi
mv "$SKILL_TMP" "$SKILL_DST"
echo "  ✓ Installed $SKILL_DST"

echo "[4/5] Configuring hooks..."
mkdir -p "$CODEX_DIR"
if [ -f "$HOOKS_FILE" ]; then
  # Only the pristine, pre-plugin file is worth keeping: a reinstall must not
  # overwrite it with output this installer wrote itself.
  if [ -e "$HOOKS_FILE.bak" ]; then
    echo "  · Existing backup kept as it is: $HOOKS_FILE.bak"
  else
    cp "$HOOKS_FILE" "$HOOKS_FILE.bak"
    echo "  ⚠ Existing hooks.json backed up to $HOOKS_FILE.bak"
  fi
fi
install_or_update_hooks_file "$HOOKS_FILE" "$INSTALL_DIR"
echo "  ✓ memsearch-mini hook entries written to $HOOKS_FILE"
# `if` so `set -e` does not abort the install: a config this edit refuses to
# touch is a warning, not a reason to leave the hooks half-wired.
if ensure_hooks_enabled "$CONFIG_FILE"; then
  echo "  ✓ hooks = true under [features] in $CONFIG_FILE"
else
  echo "  ⚠ $CONFIG_FILE left untouched — set hooks = true under [features] by hand"
fi

echo "[5/5] Setting permissions..."
if chmod +x "$INSTALL_DIR/bin/memsearch-mini" "$INSTALL_DIR/hooks/"*.sh "$INSTALL_DIR/codex/install.sh" 2>/dev/null; then
  echo "  ✓ Scripts marked executable"
else
  echo "  ⚠ Could not change permissions (read-only checkout?) — the hooks are run through bash, so this is harmless"
fi

echo ""
echo "=== Installation complete ==="
echo ""
echo "What happens automatically:"
echo "  • SessionStart: reports index status and injects recent memory"
echo "  • Stop: summarizes the turn and appends it to today's journal"
echo "  • UserPromptSubmit: reminds Codex that recall is available"
echo "  • memory-recall skill: searches past sessions when it is relevant"
echo ""
echo "Memory files:   <project>/.memsearch-mini/memory/*.md"
echo "Index:          <project>/.memsearch-mini/index.db  (derived, rebuildable)"
echo "Runtime home:   $MEMSEARCH_MINI_HOME"
echo "                everything downloaded lives there: venv, uv cache, python,"
echo "                embedding models and config.toml. Nothing else is written"
echo "                outside your projects, so uninstalling reclaims it all."
echo "Sync log:       $VENV.log"
echo "Hooks:          $HOOKS_FILE"
echo "Skill:          $SKILL_DST"
echo ""
echo "To uninstall:"
echo "  bash $INSTALL_DIR/uninstall.sh            # unwire the hooks, remove the skill"
echo "  bash $INSTALL_DIR/uninstall.sh --purge    # the same, plus rm -rf $MEMSEARCH_MINI_HOME"
echo "  then delete this checkout; project journals under .memsearch-mini/ stay untouched"
