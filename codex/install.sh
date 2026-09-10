#!/usr/bin/env bash
# Installer for the memsearch Codex CLI plugin.
#
#   bash codex/install.sh
#
# Copies the memory-recall skill into ~/.agents/skills, merges the memsearch
# hook entries into ~/.codex/hooks.json and enables the hooks feature flag.
# Set MEMSEARCH_SKIP_SYNC=1 to skip the blocking runtime sync.
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
CODEX_DIR="$HOME/.codex"
HOOKS_FILE="$CODEX_DIR/hooks.json"
CONFIG_FILE="$CODEX_DIR/config.toml"
SKILL_SRC="$INSTALL_DIR/codex/skills/memory-recall"
SKILL_DST="$HOME/.agents/skills/memory-recall"
MEMSEARCH_HOME="${MEMSEARCH_HOME:-$HOME/.memsearch}"
# Ask the library for the environment path instead of duplicating its hashing
# rule; a subshell keeps this script's `set -e` away from the sourced file.
VENV="$(bash -c 'source "$1/hooks/common.sh"; printf "%s\n" "$VENV"' _ "$INSTALL_DIR" 2>/dev/null || true)"
[ -n "$VENV" ] || VENV="$MEMSEARCH_HOME/venvs"

# The JSON/TOML edits below are the only Python this installer needs. A system
# python3 runs them when there is one; otherwise the interpreter uv manages for
# this checkout does, so the installer never depends on a system Python.
run_python() {
  if command -v python3 >/dev/null 2>&1; then
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
import re
import sys

path = Path(sys.argv[1])
if not path.exists():
    path.write_text("[features]\nhooks = true\n")
    raise SystemExit

text = path.read_text()

features_match = re.search(r"(?m)^\[features\]\s*$", text)
if features_match:
    next_section = re.search(r"(?m)^\[[^]]+\]\s*$", text[features_match.end():])
    block_end = len(text) if next_section is None else features_match.end() + next_section.start()
    block = text[features_match.end():block_end]
    block = re.sub(r"(?m)^codex_hooks\s*=.*\n?", "", block)
    if re.search(r"(?m)^hooks\s*=", block):
        block = re.sub(r"(?m)^hooks\s*=.*$", "hooks = true", block)
    else:
        if block and not block.startswith("\n"):
            block = "\n" + block
        block = "\nhooks = true" + block
    text = text[:features_match.end()] + block + text[block_end:]
else:
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n[features]\nhooks = true\n"

path.write_text(text)
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


def strip_old_memsearch(entries, script_name):
    # Two markers: the upstream layout, and this plugin's layout at any
    # checkout path (so a moved or renamed clone leaves no duplicate behind).
    markers = (f"plugins/codex/hooks/{script_name}", f"/hooks/{script_name}")
    cleaned = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hooks = []
        for hook in entry.get("hooks", []):
            command = hook.get("command", "") if isinstance(hook, dict) else ""
            if any(marker in command for marker in markers):
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
    cleaned = strip_old_memsearch(hooks.get(event, []), script)
    cleaned.append(
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f"bash {install_dir}/hooks/{script} codex",
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

echo "=== memsearch Codex CLI plugin installer ==="
echo "Checkout: $INSTALL_DIR"
echo ""

echo "[1/5] Checking uv..."
if ! command -v uv >/dev/null 2>&1; then
  echo "  ✗ uv not found. memsearch runs its CLI through uv; install it first:"
  echo "    https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi
echo "  ✓ uv found: $(command -v uv)"

echo "[2/5] Syncing the runtime (first run downloads the embedding stack)..."
if [ "${MEMSEARCH_SKIP_SYNC:-}" = "1" ]; then
  echo "  ⚠ MEMSEARCH_SKIP_SYNC=1 — skipped; the first session syncs in the background"
else
  bash "$INSTALL_DIR/bin/memsearch" --sync
fi

echo "[3/5] Installing the memory-recall skill..."
mkdir -p "$HOME/.agents/skills"
if [ -d "$SKILL_DST" ] || [ -L "$SKILL_DST" ]; then
  echo "  ⚠ Existing memory-recall skill found — replacing"
  rm -rf "$SKILL_DST"
fi
cp -r "$SKILL_SRC" "$SKILL_DST"
replace_text_in_file "$SKILL_DST/SKILL.md" "__INSTALL_DIR__" "$INSTALL_DIR"
echo "  ✓ Installed $SKILL_DST"

echo "[4/5] Configuring hooks..."
mkdir -p "$CODEX_DIR"
if [ -f "$HOOKS_FILE" ]; then
  cp "$HOOKS_FILE" "$HOOKS_FILE.bak"
  echo "  ⚠ Existing hooks.json backed up to $HOOKS_FILE.bak"
fi
install_or_update_hooks_file "$HOOKS_FILE" "$INSTALL_DIR"
echo "  ✓ memsearch hook entries written to $HOOKS_FILE"
ensure_hooks_enabled "$CONFIG_FILE"
echo "  ✓ hooks = true under [features] in $CONFIG_FILE"

echo "[5/5] Setting permissions..."
if chmod +x "$INSTALL_DIR/bin/memsearch" "$INSTALL_DIR/hooks/"*.sh "$INSTALL_DIR/codex/install.sh" 2>/dev/null; then
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
echo "Memory files:   <project>/.memsearch/memory/*.md"
echo "Index:          <project>/.memsearch/index.db  (derived, rebuildable)"
echo "Runtime home:   $MEMSEARCH_HOME"
echo "                everything downloaded lives there: venv, uv cache, python,"
echo "                embedding models and config.toml. Nothing else is written"
echo "                outside your projects, so uninstalling reclaims it all."
echo "Sync log:       $VENV.log"
echo "Hooks:          $HOOKS_FILE"
echo "Skill:          $SKILL_DST"
echo ""
echo "To uninstall:"
echo "  bash $INSTALL_DIR/uninstall.sh            # unwire the hooks, remove the skill"
echo "  bash $INSTALL_DIR/uninstall.sh --purge    # the same, plus rm -rf $MEMSEARCH_HOME"
echo "  then delete this checkout; project journals under .memsearch/ stay untouched"
