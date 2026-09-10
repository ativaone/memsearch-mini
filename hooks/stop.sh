#!/usr/bin/env bash
# Stop launcher. Never `set -e`: it must always print one JSON object.
set -uo pipefail
PLATFORM="${1:-claude}"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
hook_guard
have_uv || {
  printf '%s\n' '{"systemMessage": "[memsearch] uv not found on PATH — memory disabled (https://docs.astral.sh/uv/)"}'
  exit 0
}
# No sync here: the first install belongs to SessionStart, and this turn is lost
# on purpose rather than blocking the host on a 90 MB download.
venv_ready || {
  printf '%s\n' '{}'
  exit 0
}
# stdin is inherited untouched: Python reads the payload.
exec "$ROOT/bin/memsearch" hook stop --platform "$PLATFORM"
