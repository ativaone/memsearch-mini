#!/usr/bin/env bash
# SessionStart launcher. Never `set -e`: it must always print one JSON object.
set -uo pipefail
PLATFORM="${1:-claude}"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
hook_guard
have_uv || {
  printf '%s\n' '{"systemMessage": "[memsearch-mini] uv not found on PATH — memory disabled (https://docs.astral.sh/uv/)"}'
  exit 0
}
if ! venv_ready; then
  sync_detached "$PLATFORM"
  printf '%s\n' '{"systemMessage": "[memsearch-mini] installing runtime in the background — memory available from the next session"}'
  exit 0
fi
# stdin is inherited untouched: Python reads the payload.
exec "$ROOT/bin/memsearch-mini" hook session-start --platform "$PLATFORM"
