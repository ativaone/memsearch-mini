#!/usr/bin/env bash
# memsearch-mini uninstaller.
#
#   bash uninstall.sh            unwire the hooks, keep the runtime home
#   bash uninstall.sh --purge    also delete $MEMSEARCH_HOME (default ~/.memsearch)
#
# Never `set -e`: every step reports what it did and the next one still runs.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
MEMSEARCH_HOME="${MEMSEARCH_HOME:-$HOME/.memsearch}"
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

# Removes only entries whose command names one of our three hook scripts, by
# the same two markers install.sh uses: the upstream layout and any checkout.
strip_codex_hooks() {
  run_python "$1" <<'PY'
from pathlib import Path
import json
import os
import sys

path = Path(sys.argv[1])
scripts = ("session-start.sh", "user-prompt-submit.sh", "stop.sh")
markers = [f"plugins/codex/hooks/{name}" for name in scripts] + [f"/hooks/{name}" for name in scripts]
removed = []


def ours(command):
    return isinstance(command, str) and any(marker in command for marker in markers)


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
    print("  · no memsearch entries in hooks.json")
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
echo "Runtime home: $MEMSEARCH_HOME"
echo ""

echo "[1/4] Codex hook entries..."
if [ -f "$CODEX_HOOKS" ]; then
  strip_codex_hooks "$CODEX_HOOKS"
else
  echo "  · no $CODEX_HOOKS — nothing to unwire"
fi
echo "  · [features] hooks in $CODEX_CONFIG left as it is: other tools may rely on it"

echo "[2/4] Codex skill..."
if [ -f "$SKILL_DIR/SKILL.md" ] && grep -q "bin/memsearch" "$SKILL_DIR/SKILL.md" 2>/dev/null; then
  rm -rf "$SKILL_DIR" && echo "  ✓ removed $SKILL_DIR"
elif [ -e "$SKILL_DIR" ]; then
  echo "  ⚠ $SKILL_DIR is not a memsearch skill — left untouched"
else
  echo "  · no skill installed at $SKILL_DIR"
fi

echo "[3/4] Claude Code — run these two commands inside Claude Code:"
echo "    /plugin uninstall memsearch-mini@ativaone"
echo "    /plugin marketplace remove ativaone"

echo "[4/4] Runtime home..."
if [ -d "$MEMSEARCH_HOME" ]; then
  MS_SIZE="$(du -sh "$MEMSEARCH_HOME" 2>/dev/null | cut -f1)"
  echo "  $MEMSEARCH_HOME holds ${MS_SIZE:-?} — venvs, uv cache, embedding models, python, config.toml"
  if [ "$PURGE" = "1" ]; then
    if rm -rf "$MEMSEARCH_HOME"; then
      echo "  ✓ removed $MEMSEARCH_HOME"
    else
      echo "  ✗ could not remove $MEMSEARCH_HOME"
    fi
  else
    echo "  · kept. To reclaim it later (this also deletes config.toml):"
    echo "      rm -rf $MEMSEARCH_HOME"
  fi
else
  echo "  · $MEMSEARCH_HOME does not exist"
fi

echo ""
echo "Project journals are never touched: <project>/.memsearch/memory/*.md stays."
echo "To drop one project's derived index:"
echo "  rm -f <project>/.memsearch/index.db <project>/.memsearch/index.db-wal <project>/.memsearch/index.db-shm <project>/.memsearch/index.lock"
