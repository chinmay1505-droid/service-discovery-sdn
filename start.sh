#!/usr/bin/env bash
# start.sh — Launch the Service Discovery SDN Web GUI & Backend
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=${1:-8000}
MODE=${SDN_MODE:-standalone}
if [ "$MODE" = "mininet" ]; then
  REGISTRY="Mininet Registry: ${SDN_REGISTRY_IP:-10.0.0.100} (UDP 5000/5001)"
else
  REGISTRY="Embedded Registry: UDP 5000 (adverts) / 5001 (queries)"
fi

echo "================================================================"
echo "          SERVICE DISCOVERY SDN — CONTROL SYSTEM               "
echo "================================================================"
echo "  $REGISTRY"
echo "  Web GUI & API:     http://localhost:$PORT                    "
echo "================================================================"

exec bash "$SCRIPT_DIR/start_backend.sh" "$PORT"
