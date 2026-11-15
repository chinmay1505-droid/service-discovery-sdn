"""FastAPI application — Service Discovery SDN Backend.

Connects the web GUI to the real networking layer:
  - Low-level socket discovery and UDP communication (client_runner)
  - Live embedded registry bridge (BridgedRegistry on UDP 5000/5001)
  - Real-time thread-safe event bus with WebSocket delivery (/ws)
  - SDN flow inspection via ovs-ofctl or controller state (/api/flows)
  - Mininet / network topology visualization (/api/topology)
  - Server process controls (/api/servers/start, /api/servers/stop)
  - Robustness scenario lab (/api/robustness/run)
  - Performance benchmarking & baseline comparison (/api/performance/run)
  - Automated 17-point verification suite (/api/tests/run)
"""
import asyncio
import json
import os
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---- resolve project paths ------------------------------------------------
_BACKEND = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_BACKEND, ".."))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from common import SERVICE_TTL, SERVICE_PORT, REGISTRY_QUERY_PORT, REGISTRY_ADVERT_PORT  # noqa: E402
from services import event_bus                                                            # noqa: E402
from services.registry_bridge import BridgedRegistry                                      # noqa: E402
from services import client_runner                                                        # noqa: E402
from services import process_manager                                                      # noqa: E402
from services import flow_inspector                                                       # noqa: E402
from services import topology_service                                                     # noqa: E402
from services import robustness_runner                                                    # noqa: E402
from services import benchmark_service                                                    # noqa: E402
from services import test_runner                                                          # noqa: E402

# ---- globals ---------------------------------------------------------------
_registry: BridgedRegistry | None = None
_registry_thread: threading.Thread | None = None

FRONTEND_DIR = os.path.join(_ROOT, "frontend")


# ---- Pydantic Request Models -----------------------------------------------
class DiscoverRequest(BaseModel):
    service: str = Field(default="chat", description="Service type, or '*' for all")
    registry_ip: str = Field(default="127.0.0.1", description="Registry IP address")
    timeout: float = Field(default=1.0, ge=0.1, le=10.0, description="Per-attempt UDP socket timeout in seconds")
    retries: int = Field(default=3, ge=1, le=10, description="Number of retransmission attempts")
    use: bool = Field(default=False, description="Automatically send service request to first discovered server")
    message: str = Field(default="hello", description="Payload message for service call")


class ConnectRequest(BaseModel):
    ip: str = Field(..., description="Server IP address")
    port: int = Field(default=SERVICE_PORT, ge=1, le=65535, description="Server UDP port")
    message: str = Field(default="hello", description="Service datagram payload")
    timeout: float = Field(default=2.0, ge=0.1, le=10.0, description="UDP socket timeout in seconds")
    retries: int = Field(default=2, ge=1, le=5, description="Number of attempts")


class ServerStartRequest(BaseModel):
    name: str = Field(..., description="Server host name, e.g. srv1")
    service: str = Field(..., description="Service type offered, e.g. chat, files")
    registry_ip: str = Field(default="127.0.0.1", description="Registry target IP")
    port: int = Field(default=SERVICE_PORT, ge=1, le=65535, description="UDP port for service")
    interval: float = Field(default=5.0, ge=0.5, le=60.0, description="Advertisement interval in seconds")
    ttl: float = Field(default=SERVICE_TTL, ge=1.0, le=300.0, description="TTL timeout in seconds")
    no_advertise: bool = Field(default=False, description="Start without advertising (tests SDN rogue blocking)")


class ServerStopRequest(BaseModel):
    name: str = Field(..., description="Server host name to stop")
    hard: bool = Field(default=False, description="True=SIGKILL crash simulation, False=SIGTERM graceful shutdown")


class RobustnessRequest(BaseModel):
    scenario: str = Field(default="A", description="Scenario: 'A' (Crash), 'B' (Rogue), 'C' (Unknown), 'D' (Retries)")
    registry_ip: str = Field(default="127.0.0.1")
    target: Optional[str] = None


class BenchmarkRequest(BaseModel):
    registry_ip: str = Field(default="127.0.0.1")
    trials: int = Field(default=10, ge=1, le=100)


# ---- lifespan (startup / shutdown) ----------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _registry, _registry_thread

    # Give the event bus the running asyncio loop so sync threads can post to it.
    event_bus.set_loop(asyncio.get_running_loop())

    # Mininet owns its own registry in the `reg` namespace. Do not start a
    # second registry in the host namespace and present it as the same runtime.
    if _detect_mode() == "standalone":
        _registry = BridgedRegistry(bind_ip="0.0.0.0", default_ttl=SERVICE_TTL)
        _registry_thread = threading.Thread(target=_registry.run, daemon=True, name="registry")
        _registry_thread.start()

    event_bus.emit("SYSTEM", "BACKEND_STARTED", {
        "message": ("Registry embedded on UDP 5000/5001" if _detect_mode() == "standalone"
                    else f"Using Mininet registry at {_mininet_registry_ip()} (no host registry started)"),
        "mode": _detect_mode(),
    })

    yield  # ← app serves requests here

    # Graceful shutdown.
    if _registry:
        _registry.running = False
    process_manager.stop_all()
    event_bus.emit("SYSTEM", "BACKEND_STOPPED", {"message": "Shutting down"})


# ---- app -------------------------------------------------------------------
app = FastAPI(title="Service Discovery SDN", version="1.0.0", lifespan=lifespan)


# ---- static files ----------------------------------------------------------
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
    if _detect_mode() == "mininet":
        reg_probe = client_runner.run_discover(_mininet_registry_ip(), "*", timeout=0.2, retries=1)
        reg_ok = bool(reg_probe.get("ok"))
        server_count = reg_probe.get("count", 0) if reg_ok else 0
    else:
        reg_probe = client_runner.run_discover("127.0.0.1", "*", timeout=0.2, retries=1)
        reg_ok = bool(_registry is not None and _registry.running and reg_probe.get("ok"))
        server_count = reg_probe.get("count", 0) if reg_ok else 0

    return {
        "registry": {
            "running": reg_ok,
            "address": _mininet_registry_ip() if _detect_mode() == "mininet" else "0.0.0.0",
            "advert_port": REGISTRY_ADVERT_PORT,
            "query_port": REGISTRY_QUERY_PORT,
        },
        "controller": _probe_controller(),
        "servers_online": server_count,
        "mode": _detect_mode(),
        "events_count": len(event_bus.get_history(500)),
        "ts": time.strftime("%H:%M:%S"),
    }


# ---- REST: registry --------------------------------------------------------
@app.get("/api/registry")
async def api_registry():
    if _detect_mode() == "mininet":
        discovery = client_runner.run_discover(_mininet_registry_ip(), "*", timeout=0.5, retries=2)
        entries = [{**entry, "status": "ACTIVE"} for entry in discovery.get("servers", [])]
        return {"entries": sorted(entries, key=lambda x: x.get("name", "")),
                "count": len(entries), "available": bool(discovery.get("ok")),
                "error": discovery.get("error"), "source": "Mininet registry UDP query"}
    if not _registry:
        return {"entries": [], "count": 0}

    now = time.time()
    with _registry.lock:
        entries = [
            {
                "name": e["name"],
                "service": e["service"],
                "ip": e["ip"],
                "port": e["port"],
                "ttl": e["ttl"],
                "last_seen": round(now - e["last_seen"], 1),
                "adverts": e.get("adverts", 0),
                "first_seen": round(now - e.get("first_seen", now), 1),
                "status": "ACTIVE",
            }
            for e in _registry.table.values()
        ]

    return {
        "entries": sorted(entries, key=lambda x: x["name"]),
        "count": len(entries),
        "ts": time.strftime("%H:%M:%S"),
    }


# ---- REST: services summary ------------------------------------------------
@app.get("/api/services")
async def api_services():
    """Summary of unique services registered and server counts."""
    if _detect_mode() == "mininet":
        snapshot = await api_registry()
        services_map = {}
        for e in snapshot["entries"]:
            name = e.get("service", "")
            services_map.setdefault(name, {"service": name, "servers": [], "count": 0})
            services_map[name]["servers"].append({"name": e.get("name"), "ip": e.get("ip"), "port": e.get("port")})
            services_map[name]["count"] += 1
        items = sorted(services_map.values(), key=lambda x: x["service"])
        return {"services": items, "count": len(items), "available": snapshot.get("available", False)}
    if not _registry:
        return {"services": [], "count": 0}

    now = time.time()
    services_map = {}
    with _registry.lock:
        for e in _registry.table.values():
            s_name = e["service"]
            if s_name not in services_map:
                services_map[s_name] = {"service": s_name, "servers": [], "count": 0}
            services_map[s_name]["servers"].append({
                "name": e["name"],
                "ip": e["ip"],
                "port": e["port"],
                "age": round(now - e["last_seen"], 1),
            })
            services_map[s_name]["count"] += 1

    s_list = sorted(services_map.values(), key=lambda x: x["service"])
    return {"services": s_list, "count": len(s_list)}


@app.get("/api/services/{service}")
async def api_service_detail(service: str):
    """Detailed server list for a specific service name."""
    if _detect_mode() == "mininet":
        discovery = client_runner.run_discover(_mininet_registry_ip(), service, timeout=0.5, retries=2)
        return {"service": service, "servers": discovery.get("servers", []),
                "count": discovery.get("count", 0), "available": bool(discovery.get("ok"))}
    if not _registry:
        return {"service": service, "servers": [], "count": 0}

    hits = _registry.lookup(service)
    return {"service": service, "servers": hits, "count": len(hits)}


# ---- REST: servers ---------------------------------------------------------
@app.get("/api/servers")
async def api_servers():
    """List of all servers currently registered."""
    return await api_registry()


@app.get("/api/servers/managed")
async def api_managed_servers():
    """List of server processes managed by the backend."""
    return {"managed": process_manager.get_managed_servers()}


@app.post("/api/servers/start")
async def api_server_start(req: ServerStartRequest):
    """Start a real service server subprocess."""
    if _detect_mode() == "mininet":
        raise HTTPException(status_code=409, detail="Server controls use host processes and are disabled in Mininet mode; start/stop servers through the Mininet CLI.")
    res = process_manager.start_server(
        name=req.name,
        service=req.service,
        registry_ip=req.registry_ip,
        port=req.port,
        interval=req.interval,
        ttl=req.ttl,
        no_advertise=req.no_advertise,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    return res


@app.post("/api/servers/stop")
async def api_server_stop(req: ServerStopRequest):
    """Stop or crash a service server subprocess."""
    if _detect_mode() == "mininet":
        raise HTTPException(status_code=409, detail="Server controls use host processes and are disabled in Mininet mode; stop servers through the Mininet CLI.")
    res = process_manager.stop_server(name=req.name, hard=req.hard)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    return res


# ---- REST: discovery -------------------------------------------------------
@app.post("/api/discover")
async def api_discover(req: DiscoverRequest):
    """
    Perform real service discovery through low-level UDP sockets.
    Queries the registry on UDP 5001. If use=True and a server is discovered,
    also performs an end-to-end service call on UDP 7000.
    """
    reg_ip = _mininet_registry_ip() if _detect_mode() == "mininet" and req.registry_ip == "127.0.0.1" else req.registry_ip
    disc_res = client_runner.run_discover(
        registry_ip=reg_ip,
        service=req.service,
        timeout=req.timeout,
        retries=req.retries,
    )

    result = dict(disc_res)

    # If requested and server(s) found, perform service invocation
    if req.use and disc_res.get("ok") and disc_res.get("servers"):
        first_server = disc_res["servers"][0]
        conn_res = client_runner.run_connect(
            ip=first_server["ip"],
            port=first_server["port"],
            message=req.message,
            timeout=2.0,
            retries=2,
        )
        result["used"] = first_server["name"]
        result["used_target"] = f"{first_server['ip']}:{first_server['port']}"
        result["service_reply"] = conn_res.get("reply")
        result["service_ok"] = conn_res.get("ok")
        result["service_latency_ms"] = conn_res.get("latency_ms")

    return result


# ---- REST: direct service call ---------------------------------------------
@app.post("/api/connect")
async def api_connect(req: ConnectRequest):
    """Send a real UDP service request to a specific IP:port and return echo reply."""
    return client_runner.run_connect(
        ip=req.ip,
        port=req.port,
        message=req.message,
        timeout=req.timeout,
        retries=req.retries,
    )


# ---- REST: SDN flows -------------------------------------------------------
@app.get("/api/flows")
async def api_flows():
    """Retrieve live OpenFlow 1.3 flow table from Open vSwitch or controller state."""
    return flow_inspector.get_flows()


# ---- REST: topology --------------------------------------------------------
@app.get("/api/topology")
async def api_topology():
    """Retrieve live network topology graph with real-time status."""
    table = _registry.table if _registry else {}
    ctrl_status = _probe_controller()
    mode = _detect_mode()
    if mode == "standalone":
        registry_probe = client_runner.run_discover("127.0.0.1", "*", timeout=0.2, retries=1)
        ctrl_status["registry_online"] = bool(registry_probe.get("ok"))
    if mode == "mininet":
        discovery = client_runner.run_discover(_mininet_registry_ip(), "*", timeout=0.3, retries=1)
        ctrl_status["registry_online"] = bool(discovery.get("ok"))
        for entry in discovery.get("servers", []):
            table[(entry.get("ip"), int(entry.get("port", SERVICE_PORT)), entry.get("service"))] = entry
    flow_state = flow_inspector.get_flows()
    return topology_service.get_topology(table, ctrl_status, mode=mode,
                                         flows_available=flow_state.get("available", False))


# ---- REST: robustness lab --------------------------------------------------
@app.post("/api/robustness/run")
async def api_robustness(req: RobustnessRequest):
    """Run one of the 4 core robustness scenarios."""
    sc = req.scenario.upper()
    reg_ip = _mininet_registry_ip() if _detect_mode() == "mininet" and req.registry_ip == "127.0.0.1" else req.registry_ip
    if _detect_mode() == "mininet" and sc in ("A", "B", "D"):
        raise HTTPException(status_code=409, detail="This robustness runner controls host processes and cannot inject faults inside Mininet namespaces. Run the scenario from the Mininet CLI instead.")
    if sc == "A":
        return robustness_runner.run_scenario_a(registry_ip=reg_ip, server_name=req.target or "srv1")
    elif sc == "B":
        return robustness_runner.run_scenario_b(registry_ip=reg_ip)
    elif sc == "C":
        return robustness_runner.run_scenario_c(registry_ip=reg_ip, unknown_service=req.target or "printer")
    elif sc == "D":
        return robustness_runner.run_scenario_d(registry_ip=reg_ip)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown scenario '{req.scenario}'. Expected A, B, C, or D.")


# ---- REST: performance benchmarking ----------------------------------------
@app.post("/api/performance/run")
async def api_performance(req: BenchmarkRequest):
    """Run real latency and throughput trials against discovery and services."""
    reg_ip = _mininet_registry_ip() if _detect_mode() == "mininet" and req.registry_ip == "127.0.0.1" else req.registry_ip
    return benchmark_service.run_benchmark(registry_ip=reg_ip, trials=req.trials)


@app.get("/api/performance/baseline")
async def api_baseline():
    """Return technical comparison between Static Baseline and Dynamic SDN."""
    return benchmark_service.get_baseline_comparison()


# ---- REST: test suite ------------------------------------------------------
@app.post("/api/tests/run")
async def api_run_tests():
    """Run automated 17-point verification suite."""
    return test_runner.run_all_tests(registry_ip="127.0.0.1")


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
    """Return explicitly selected runtime; package presence is not runtime state."""
    return "mininet" if os.environ.get("SDN_MODE", "standalone").strip().lower() == "mininet" else "standalone"


def _mininet_registry_ip() -> str:
    return os.environ.get("SDN_REGISTRY_IP", "10.0.0.100")


# ---- entry point -----------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
