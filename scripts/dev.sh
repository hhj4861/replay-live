#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -x /opt/homebrew/opt/node@24/bin/node ]; then
  export PATH="/opt/homebrew/opt/node@24/bin:$PATH"
fi
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 8080 --no-access-log &
api_pid=$!
trap 'kill "$api_pid" 2>/dev/null || true' EXIT INT TERM
cd web
npm run dev -- --host 127.0.0.1 --port 3000
