#!/usr/bin/env bash
# start_backend.sh — start the FastAPI backend (works on Ubuntu/Linux)
# Usage:  bash start_backend.sh [--port 8000]
set -e
PORT=${1:-8000}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/backend"

# Install backend deps if not already installed
if ! python3 -c "import fastapi, uvicorn" 2>/dev/null; then
    echo "Installing backend dependencies..."
    pip3 install -r requirements.txt
fi

echo "Starting Service Discovery SDN backend on http://0.0.0.0:$PORT"
exec python3 -m uvicorn app:app --host 0.0.0.0 --port "$PORT" --log-level info

