#!/usr/bin/env bash
# start.sh — Launch the Service Discovery SDN Web GUI & Backend
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=${1:-8000}

echo "================================================================"
echo "          SERVICE DISCOVERY SDN — CONTROL SYSTEM               "
echo "================================================================"
echo "  Embedded Registry: UDP 5000 (adverts) / UDP 5001 (queries)   "
echo "  Web GUI & API:     http://localhost:$PORT                    "
echo "================================================================"

exec bash "$SCRIPT_DIR/start_backend.sh" "$PORT"
