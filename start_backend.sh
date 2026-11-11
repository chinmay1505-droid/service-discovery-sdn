#!/usr/bin/env bash
# start_backend.sh — start the FastAPI backend (works on Ubuntu/Linux)
# Usage:  bash start_backend.sh [--port 8000]
set -e
PORT=${1:-8000}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/backend"

# Select python binary (prefer local .venv if present)
if [ -f "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PY="$SCRIPT_DIR/.venv/bin/python3"
else
    PY="python3"
fi

# Install backend deps if not already installed
if ! "$PY" -c "import fastapi, uvicorn" 2>/dev/null; then
    echo "Installing backend dependencies..."
    "$PY" -m pip install -r requirements.txt
fi

echo "Starting Service Discovery SDN backend on http://0.0.0.0:$PORT"
exec "$PY" -m uvicorn app:app --host 0.0.0.0 --port "$PORT" --log-level info

