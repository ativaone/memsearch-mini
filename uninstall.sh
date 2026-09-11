#!/usr/bin/env bash
# memsearch-mini uninstaller.
#
#   bash uninstall.sh            unwire the hooks, keep the runtime home
#   bash uninstall.sh --purge    also delete $MEMSEARCH_MINI_HOME (default ~/.memsearch-mini)
#
# Never `set -e`: every step reports what it did and the next one still runs.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
MEMSEARCH_MINI_HOME="${MEMSEARCH_MINI_HOME:-$HOME/.memsearch-mini}"
# The `uv run` fallback in run_python must never build an environment inside the
# checkout being retired: without UV_PROJECT_ENVIRONMENT uv creates $ROOT/.venv.
# Ask the library for the same path the hooks use; a subshell keeps a sourcing
# failure from taking this script down, and a value the user set still wins.
VENV="$(bash -c 'source "$1/hooks/common.sh"; printf "%s\n" "$VENV"' _ "$ROOT" 2>/dev/null || true)"
if [ -n "$VENV" ]; then
  export UV_PROJECT_ENVIRONMENT="$VENV"
fi
UV_CACHE_DIR="${UV_CACHE_DIR:-$MEMSEARCH_MINI_HOME/uv-cache}"
UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$MEMSEARCH_MINI_HOME/python}"
export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR
CODEX_HOOKS="$HOME/.codex/hooks.json"
CODEX_CONFIG="$HOME/.codex/config.toml"
SKILL_DIR="$HOME/.agents/skills/memory-recall"
PURGE=0
case "${1:-}" in
  "") ;;
  --purge) PURGE=1 ;;
  *) echo "usage: bash uninstall.sh [--purge]" >&2; exit 2 ;;
esac

run_python() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$@"
  else
    uv run --project "$ROOT" --frozen --no-sync python - "$@"
  fi
  return $?
}

# Removes only entries whose command names one of our three hook scripts, by the
# same two markers install.sh uses: the upstream layout, and any checkout whose
# command also ends with our " codex" argument.
strip_codex_hooks() {
  run_python "$1" <<'PY'
from pathlib import Path
import json
import os
import sys

path = Path(sys.argv[1])
scripts = ("session-start.sh", "user-prompt-submit.sh", "stop.sh")
removed = []


def ours(command):
    """The bare `/hooks/<script>` marker matches any third-party tool that names
    a script the same way, so it additionally requires our trailing platform
    argument — every entry the installer has ever written ends with " codex"."""
    if not isinstance(command, str):
        return False
    for name in scripts:
        if f"plugins/codex/hooks/{name}" in command:
            return True
        if f"/hooks/{name}" in command and command.rstrip().endswith(" codex"):
            return True
    return False


try:
    data = json.loads(path.read_text())
except Exception:
    print("  ! hooks.json is not valid JSON — left untouched")
    raise SystemExit(0)

if isinstance(data, list):
    kept = []
    for item in data:
        command = item.get("command", "") if isinstance(item, dict) else ""
        if ours(command):
            removed.append(command)
        else:
            kept.append(item)
    data = kept
elif isinstance(data, dict) and isinstance(data.get("hooks"), dict):
    for event in list(data["hooks"]):
        cleaned = []
        for entry in data["hooks"][event]:
            if not isinstance(entry, dict):
                cleaned.append(entry)
                continue
            hooks = []
            for hook in entry.get("hooks", []):
                command = hook.get("command", "") if isinstance(hook, dict) else ""
                if ours(command):
                    removed.append(command)
                else:
                    hooks.append(hook)
            if hooks:
                copied = dict(entry)
                copied["hooks"] = hooks
                cleaned.append(copied)
        if cleaned:
            data["hooks"][event] = cleaned
        else:
            del data["hooks"][event]
else:
    print("  ! unrecognised hooks.json shape — left untouched")
    raise SystemExit(0)

if not removed:
    print("  · no memsearch-mini entries in hooks.json")
    raise SystemExit(0)

tmp = path.with_name(path.name + ".tmp")
tmp.write_text(json.dumps(data, indent=2) + "\n")
os.replace(tmp, path)
for command in removed:
    print(f"  ✓ removed: {command}")
PY
  return $?
}

echo "=== memsearch-mini uninstaller ==="
echo "Checkout:     $ROOT"
echo "Runtime home: $MEMSEARCH_MINI_HOME"
echo ""

echo "[1/4] Codex hook entries..."
if [ -f "$CODEX_HOOKS" ]; then
  strip_codex_hooks "$CODEX_HOOKS"
else
  echo "  · no $CODEX_HOOKS — nothing to unwire"
fi
echo "  · [features] hooks in $CODEX_CONFIG left as it is: other tools may rely on it"
for _bak in "$CODEX_HOOKS.bak" "$CODEX_CONFIG.bak"; do
  [ -f "$_bak" ] && echo "  · pre-plugin backup kept: $_bak — delete it by hand if unwanted"
done

echo "[2/4] Codex skill..."
if [ -f "$SKILL_DIR/SKILL.md" ] && grep -q "bin/memsearch-mini" "$SKILL_DIR/SKILL.md" 2>/dev/null; then
  rm -rf "$SKILL_DIR" && echo "  ✓ removed $SKILL_DIR"
elif [ -e "$SKILL_DIR" ]; then
  echo "  ⚠ $SKILL_DIR is not a memsearch-mini skill — left untouched"
else
  echo "  · no skill installed at $SKILL_DIR"
fi

echo "[3/4] Claude Code — run these two commands inside Claude Code:"
echo "    /plugin uninstall memsearch-mini@ativaone"
echo "    /plugin marketplace remove ativaone"

echo "[4/4] Runtime home..."
if [ -d "$MEMSEARCH_MINI_HOME" ]; then
  MS_SIZE="$(du -sh "$MEMSEARCH_MINI_HOME" 2>/dev/null | cut -f1)"
  echo "  $MEMSEARCH_MINI_HOME holds ${MS_SIZE:-?} — venvs, uv cache, embedding models, python, config.toml"
  if [ "$PURGE" = "1" ]; then
    if rm -rf "$MEMSEARCH_MINI_HOME"; then
      echo "  ✓ removed $MEMSEARCH_MINI_HOME"
    else
      echo "  ✗ could not remove $MEMSEARCH_MINI_HOME"
    fi
  else
    echo "  · kept. To reclaim it later (this also deletes config.toml):"
    echo "      rm -rf $MEMSEARCH_MINI_HOME"
  fi
else
  echo "  · $MEMSEARCH_MINI_HOME does not exist"
fi

echo ""
echo "Project journals are never touched: <project>/.memsearch-mini/memory/*.md stays."
echo "To drop one project's derived index:"
echo "  rm -f <project>/.memsearch-mini/index.db <project>/.memsearch-mini/index.db-wal <project>/.memsearch-mini/index.db-shm <project>/.memsearch-mini/index.lock"
