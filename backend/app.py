"""FastAPI application — Phase 2 (backend skeleton + event bus).

Endpoints
---------
GET  /              → frontend/index.html
GET  /api/status    → system health
GET  /api/registry  → live registry table
GET  /api/events    → last N events (REST fallback)
WS   /ws            → real-time event stream

The embedded BridgedRegistry runs on the standard UDP ports (5000/5001) so the
existing CLI tools (service_server.py, client.py) work unchanged alongside the GUI.
"""
import asyncio
import json
import os
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---- resolve project paths ------------------------------------------------
_BACKEND = os.path.dirname(os.path.abspath(__file__))
_ROOT    = os.path.abspath(os.path.join(_BACKEND, ".."))
_SRC     = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from services import event_bus                         # noqa: E402
from services.registry_bridge import BridgedRegistry   # noqa: E402
from common import SERVICE_TTL                         # noqa: E402

# ---- globals ---------------------------------------------------------------
_registry: BridgedRegistry | None = None
_registry_thread: threading.Thread | None = None

FRONTEND_DIR = os.path.join(_ROOT, "frontend")


# ---- lifespan (startup / shutdown) ----------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _registry, _registry_thread

    # Give the event bus the running asyncio loop so sync threads can post to it.
    event_bus.set_loop(asyncio.get_running_loop())

    # Start the embedded registry (UDP 5000 + 5001 on all interfaces).
    _registry = BridgedRegistry(bind_ip="0.0.0.0", default_ttl=SERVICE_TTL)
    _registry_thread = threading.Thread(target=_registry.run, daemon=True, name="registry")
    _registry_thread.start()

    event_bus.emit("SYSTEM", "BACKEND_STARTED", {
        "message": "Registry embedded on UDP 5000 (adverts) / 5001 (queries)",
        "mode": _detect_mode(),
    })

    yield  # ← app serves requests here

    # Graceful shutdown.
    if _registry:
        _registry.running = False
    event_bus.emit("SYSTEM", "BACKEND_STOPPED", {"message": "Shutting down"})


# ---- app -------------------------------------------------------------------
app = FastAPI(title="Service Discovery SDN", version="0.2.0", lifespan=lifespan)


# ---- static files ----------------------------------------------------------
# Serve frontend static assets (css, js) if the directory exists.
_STATIC = os.path.join(FRONTEND_DIR, "static")
if os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")


# ---- HTML root -------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def index():
    path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.isfile(path):
        return FileResponse(path)
    return JSONResponse({"error": "frontend/index.html not found"}, status_code=404)


# ---- REST: status ----------------------------------------------------------
@app.get("/api/status")
async def api_status():
    reg_ok = _registry is not None and _registry.running
    server_count = 0
    if reg_ok:
        with _registry.lock:
            server_count = len(_registry.table)

    return {
        "registry": {
            "running": reg_ok,
            "address": "0.0.0.0",
            "advert_port": 5000,
            "query_port": 5001,
        },
        "controller": _probe_controller(),
        "servers_online": server_count,
        "mode": _detect_mode(),
        "ts": time.strftime("%H:%M:%S"),
    }


# ---- REST: registry --------------------------------------------------------
@app.get("/api/registry")
async def api_registry():
    if not _registry:
        return {"entries": [], "count": 0}

    now = time.time()
    with _registry.lock:
        entries = [
            {
                "name":      e["name"],
                "service":   e["service"],
                "ip":        e["ip"],
                "port":      e["port"],
                "ttl":       e["ttl"],
                "last_seen": round(now - e["last_seen"], 1),
                "adverts":   e.get("adverts", 0),
                "first_seen": round(now - e.get("first_seen", now), 1),
                "status":    "ONLINE",
            }
            for e in _registry.table.values()
        ]

    return {
        "entries": sorted(entries, key=lambda x: x["name"]),
        "count": len(entries),
        "ts": time.strftime("%H:%M:%S"),
    }


# ---- REST: events (non-WS fallback) ----------------------------------------
@app.get("/api/events")
async def api_events(n: int = 100):
    return {"events": event_bus.get_history(n), "count": len(event_bus.get_history(n))}


# ---- WebSocket -------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    q = event_bus.subscribe()

    # Replay recent history so the client sees what happened before connecting.
    for ev in event_bus.get_history(50):
        try:
            await ws.send_text(json.dumps(ev))
        except Exception:
            event_bus.unsubscribe(q)
            return

    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=20.0)
                await ws.send_text(json.dumps(ev))
            except asyncio.TimeoutError:
                # Send a keepalive ping so the browser doesn't close the connection.
                await ws.send_text(json.dumps({
                    "category": "PING",
                    "ts": time.strftime("%H:%M:%S"),
                    "ts_epoch": time.time(),
                }))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        event_bus.unsubscribe(q)


# ---- helpers ---------------------------------------------------------------
def _probe_controller() -> dict:
    """Check if the OS-Ken SDN controller is reachable on 127.0.0.1:6653."""
    try:
        s = socket.create_connection(("127.0.0.1", 6653), timeout=0.5)
        s.close()
        return {"running": True, "address": "127.0.0.1:6653", "protocol": "OpenFlow 1.3"}
    except Exception:
        return {"running": False, "address": "127.0.0.1:6653", "protocol": "OpenFlow 1.3"}


def _detect_mode() -> str:
    """Standalone (no Mininet) or mininet (Mininet available)."""
    try:
        import importlib.util
        if importlib.util.find_spec("mininet") is not None:
            return "mininet"
    except Exception:
        pass
    return "standalone"


# ---- entry point -----------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )

