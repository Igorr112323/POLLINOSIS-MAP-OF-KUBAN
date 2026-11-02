#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
command -v python3 >/dev/null || { echo 'Python 3.11+ is required'; exit 1; }
command -v npm >/dev/null || { echo 'Node.js 22.12+ and npm are required'; exit 1; }
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r backend/requirements.txt
if ! cmp -s frontend/package-lock.json frontend/node_modules/.pollinoz-lock; then
  (cd frontend && npm ci --prefer-offline --no-audit --no-fund && cp package-lock.json node_modules/.pollinoz-lock)
fi
(cd frontend && npm run build)
export DEMO_MODE="${DEMO_MODE:-true}" OFFLINE="${OFFLINE:-false}"
(cd backend && exec ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001) &
api=$!
trap 'kill "$api" 2>/dev/null || true' EXIT INT TERM
# Wait for the API so an early startup failure does not silently leave a broken UI.
ready=0
for ((i=0; i<60; i++)); do
  if ! kill -0 "$api" 2>/dev/null; then echo 'Backend failed to start'; exit 1; fi
  if .venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/v1/health', timeout=1).read()" 2>/dev/null; then ready=1; break; fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then echo 'Backend did not become healthy'; exit 1; fi
# Vite proxies browser requests to the local API. Ctrl+C stops both.
cd frontend
npm run dev -- --host 127.0.0.1
