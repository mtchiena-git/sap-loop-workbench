#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
PORT="${PORT:-8080}"
echo "SAP Loop Workbench -> http://127.0.0.1:$PORT  (LLM=${LLM_MODE:-mock} SAP=${SAP_MODE:-mock} EXTRACT=${EXTRACT_MODE:-mock})"
exec "$PY" -m uvicorn app:app --host 127.0.0.1 --port "$PORT"
