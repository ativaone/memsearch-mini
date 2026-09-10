#!/usr/bin/env bash
# UserPromptSubmit launcher: pure bash, never reads stdin, never starts Python.
set -uo pipefail
PLATFORM="${1:-claude}"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
hook_guard
have_uv || {
  printf '%s\n' '{"systemMessage": "[memsearch-mini] uv not found on PATH — memory disabled (https://docs.astral.sh/uv/)"}'
  exit 0
}
venv_ready || {
  printf '%s\n' '{}'
  exit 0
}
printf '%s\n' '{"systemMessage": "[memsearch-mini] Recall available if needed"}'
exit 0
