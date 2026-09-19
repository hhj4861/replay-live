#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${REPLAY_PUBLIC_ORIGINS:?Set the deployed Vercel HTTPS origin}"
exec .venv/bin/python -m uvicorn server.public_app:app --host 127.0.0.1 --port 8081 --no-access-log
