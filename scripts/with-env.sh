#!/usr/bin/env bash
# Load .env into the environment, then exec the given command.
#
# Generic, CLI-agnostic env loader. The app (os.environ) and every CLI's MCP
# layer (.mcp.json ${VAR} expansion) read from the process environment, not from
# .env directly -- so this script is the single bridge for all of them.
#
# .env stays the one source of truth (gitignored). No tool-specific config needed.
#
# Usage:
#   scripts/with-env.sh claude
#   scripts/with-env.sh gemini
#   scripts/with-env.sh green-gate --sweep
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$root/.env"

if [ -f "$env_file" ]; then
  set -a            # export everything sourced below
  # shellcheck disable=SC1090
  . "$env_file"
  set +a
  echo "[ok] loaded .env into process environment"
else
  echo "[~] no .env found (copy .env.example to .env first)"
fi

exec "$@"
