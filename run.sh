#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$HOME/.secrets/open-brain.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$HOME/.secrets/open-brain.env"
  set +a
fi
export DATABASE_URL="${DATABASE_URL:?set DATABASE_URL (or put it in ~/.secrets/open-brain.env)}"
exec "$ROOT/.venv/bin/python" "$ROOT/server.py"
