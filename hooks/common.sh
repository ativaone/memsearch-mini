#!/usr/bin/env bash
# Shared library for the memsearch-mini plugin shell (launchers and bin/memsearch-mini).
# Sourced, never executed; sourcing has no effect beyond variable assignment.
#
# Rules this file exists to keep:
#   * no `set -e` anywhere in the plugin shell — a launcher must always reach
#     its printf and exit 0;
#   * every function ends with an explicit `return`;
#   * a detached child redirects all three fds, or the hook runner keeps
#     waiting on the pipe it still holds open;
#   * everything the plugin downloads or creates outside a project lives under
#     $MEMSEARCH_MINI_HOME, so uninstalling is `rm -rf` on one directory. Neither
#     the checkout nor the user's caches are ever written to.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PLATFORM="${PLATFORM:-claude}"
MEMSEARCH_MINI_HOME="${MEMSEARCH_MINI_HOME:-$HOME/.memsearch-mini}"
export MEMSEARCH_MINI_HOME

# uv usually lives in one of these and a hook may run with a minimal PATH.
# Appended, never prepended, so a uv already on the caller's PATH still wins.
for _ms_dir in "$HOME/.local/bin" "$HOME/bin" "/usr/local/bin" "/opt/homebrew/bin"; do
  case ":$PATH:" in
    *":$_ms_dir:"*) ;;
    *) [ -d "$_ms_dir" ] && PATH="$PATH:$_ms_dir" ;;
  esac
done
export PATH
unset _ms_dir

# Wheels, interpreters and embedding models are the bulk of what an uninstall
# has to reclaim. A value the user set already wins: it is their disk.
: "${UV_CACHE_DIR:=$MEMSEARCH_MINI_HOME/uv-cache}"
: "${UV_PYTHON_INSTALL_DIR:=$MEMSEARCH_MINI_HOME/python}"
: "${HF_HOME:=$MEMSEARCH_MINI_HOME/models}"
export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR HF_HOME

# Environments are keyed by checkout path, so two clones never share one.
_venv_id() {
  local hash=""
  if command -v sha256sum >/dev/null 2>&1; then
    hash="$(printf '%s' "$ROOT" | sha256sum 2>/dev/null)"
  elif command -v shasum >/dev/null 2>&1; then
    hash="$(printf '%s' "$ROOT" | shasum -a 256 2>/dev/null)"
  fi
  hash="${hash%% *}"
  [ -n "$hash" ] || hash="000000000000"
  printf '%s' "${hash:0:12}"
  return 0
}

# The environment lives under $MEMSEARCH_MINI_HOME, never inside the checkout: a
# marketplace install is a tarball extraction that /plugin update replaces.
VENV="${UV_PROJECT_ENVIRONMENT:-$MEMSEARCH_MINI_HOME/venvs/$(_venv_id)}"
export UV_PROJECT_ENVIRONMENT="$VENV"
SYNC_STAMP="$VENV/.memsearch-mini-synced"
# Log and lock are siblings of the venv, never inside it: uv refuses to create
# a project environment in an existing directory that holds no interpreter, so
# a log written before the first sync would wedge the install permanently.
SYNC_LOG="$VENV.log"
SYNC_LOCK="$VENV.lock"

have_uv() {
  command -v uv >/dev/null 2>&1
  return $?
}

# Kill switch. Summarizer children re-enter the host CLI; they must not run hooks.
hook_guard() {
  if [ "${MEMSEARCH_MINI_DISABLE:-}" = "1" ]; then
    printf '%s\n' '{}'
    exit 0
  fi
  return 0
}

# Always `--extra onnx`, plus the configured embedding provider. Read with sed
# (no Python): this runs before the runtime exists.
extras_args() {
  local cfg="${MEMSEARCH_MINI_CONFIG:-$MEMSEARCH_MINI_HOME/config.toml}" provider=""
  local re="s/^[[:space:]]*provider[[:space:]]*=[[:space:]]*[\"']\{0,1\}\([A-Za-z0-9_-]*\).*/\1/p"
  [ -r "$cfg" ] && provider="$(sed -n "$re" "$cfg" 2>/dev/null | head -n 1)"
  printf '%s' '--extra onnx'
  case "$provider" in
    openai|google|voyage|jina|mistral|ollama|local) printf ' --extra %s' "$provider" ;;
  esac
  printf '\n'
  return 0
}

venv_ready() {
  [ -x "$VENV/bin/memsearch-mini" ] || return 1
  [ -f "$SYNC_STAMP" ] || return 1
  # The stamp holds the extras the environment was built with, so switching
  # embedding.provider in the config resyncs by itself on the next session.
  [ "$(cat "$SYNC_STAMP" 2>/dev/null)" = "$(extras_args)" ] || return 1
  [ ! "$ROOT/uv.lock" -nt "$SYNC_STAMP" ] || return 1
  [ ! "$ROOT/pyproject.toml" -nt "$SYNC_STAMP" ] || return 1
  return 0
}

_prepare_log() {
  local size=0
  mkdir -p "$MEMSEARCH_MINI_HOME" "${SYNC_LOG%/*}" 2>/dev/null
  # 2> before <: redirects apply left to right, so on the first run (no log
  # yet) the failing < would otherwise print bash's own error to the host.
  size="$(wc -c 2>/dev/null <"$SYNC_LOG")" || size=0
  case "$size" in ''|*[!0-9]*) size=0 ;; esac
  [ "$size" -gt 1048576 ] && : >"$SYNC_LOG"
  return 0
}

# Blocking sync. The mkdir lock keeps two hooks from syncing at once; the loser
# gives up at once instead of waiting, and a lock left by a crash goes stale.
sync_now() {
  local rc=0
  _prepare_log
  if ! mkdir "$SYNC_LOCK" 2>/dev/null; then
    [ -n "$(find "$SYNC_LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ] || return 1
    rm -rf "$SYNC_LOCK" 2>/dev/null
    mkdir "$SYNC_LOCK" 2>/dev/null || return 1
  fi
  printf '=== %s uv sync %s ===\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)" "$ROOT" >>"$SYNC_LOG" 2>/dev/null
  uv sync --project "$ROOT" --frozen $(extras_args) >>"$SYNC_LOG" 2>&1 && extras_args >"$SYNC_STAMP"
  rc=$?
  rmdir "$SYNC_LOCK" 2>/dev/null
  return $rc
}

# Detached first install: sync, then run session-start once so the index build
# and the model download start during this session instead of the next one.
sync_detached() {
  local platform="${1:-$PLATFORM}" runner=""
  _prepare_log
  command -v setsid >/dev/null 2>&1 && runner="setsid"
  $runner bash -c '
    . "$1/hooks/common.sh"
    sync_now || exit 1
    "$1/bin/memsearch-mini" hook session-start --platform "$2" <<<"{}" >/dev/null 2>&1
  ' memsearch-mini-sync "$ROOT" "$platform" </dev/null >>"$SYNC_LOG" 2>&1 &
  return 0
}
