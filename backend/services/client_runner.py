"""
client_runner.py — UDP discovery and service calls with event_bus emission at each step.

Mirrors client.py's discover() / call_service() logic but fires structured events at every
real network operation so the browser trace view can display them live as they happen.
"""
import os
import socket
import sys
import time
import uuid

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from common import REGISTRY_QUERY_PORT, MAX_DGRAM, encode, decode  # noqa: E402
from services import event_bus                                       # noqa: E402


def run_discover(registry_ip: str, service: str,
                 timeout: float = 1.0, retries: int = 3) -> dict:
    """
    Send a DISCOVER request to the registry.
    Events fired:
      CLIENT / DISCOVERY_STARTED
      CLIENT / DISCOVERY_SENT      (once per attempt)
      CLIENT / DISCOVERY_RETRY     (on timeout before retry)
      REGISTRY / QUERY_ANSWERED    (when reply arrives)
      CLIENT / DISCOVERY_RESULT    (success)   OR
      CLIENT / DISCOVERY_FAILED    (all retries exhausted / socket error)
    """
    event_bus.emit("CLIENT", "DISCOVERY_STARTED", {
        "service": service,
        "registry": f"{registry_ip}:{REGISTRY_QUERY_PORT}",
        "timeout_s": timeout,
        "max_retries": retries,
    })

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    req_id = uuid.uuid4().hex[:8]
    query = encode({"type": "DISCOVER", "service": service, "req_id": req_id})
    t0 = time.perf_counter()

    for attempt in range(1, retries + 1):
        event_bus.emit("CLIENT", "DISCOVERY_SENT", {
            "service": service,
            "to": f"{registry_ip}:{REGISTRY_QUERY_PORT}",
            "req_id": req_id,
            "attempt": attempt,
            "of": retries,
        })
        try:
            sock.sendto(query, (registry_ip, REGISTRY_QUERY_PORT))
        except OSError as exc:
            sock.close()
            event_bus.emit("CLIENT", "DISCOVERY_FAILED", {
                "service": service,
                "error": str(exc),
                "attempt": attempt,
            })
            return {"ok": False, "error": str(exc), "servers": [],
                    "latency_ms": 0.0, "attempts": attempt}

        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            sock.settimeout(max(0.01, deadline - time.perf_counter()))
            try:
                data, _ = sock.recvfrom(MAX_DGRAM)
            except socket.timeout:
                break
            msg = decode(data)
            if msg and msg.get("type") == "REPLY" and msg.get("req_id") == req_id:
                latency_ms = (time.perf_counter() - t0) * 1000
                servers = msg.get("servers", [])
                event_bus.emit("REGISTRY", "QUERY_ANSWERED", {
                    "service": service,
                    "count": len(servers),
                    "servers": [s["name"] for s in servers],
                    "req_id": req_id,
                })
                event_bus.emit("CLIENT", "DISCOVERY_RESULT", {
                    "service": service,
                    "count": len(servers),
                    "servers": [
                        f"{s['name']}@{s['ip']}:{s['port']}" for s in servers
                    ],
                    "server_details": servers,
                    "latency_ms": round(latency_ms, 2),
                    "attempts": attempt,
                })
                sock.close()
                return {
                    "ok": True,
                    "service": service,
                    "servers": servers,
                    "count": len(servers),
                    "latency_ms": round(latency_ms, 2),
                    "attempts": attempt,
                }

        if attempt < retries:
            event_bus.emit("CLIENT", "DISCOVERY_RETRY", {
                "service": service,
                "attempt": attempt,
                "of": retries,
                "reason": "timeout — no REPLY received",
            })

    latency_ms = (time.perf_counter() - t0) * 1000
    sock.close()
    event_bus.emit("CLIENT", "DISCOVERY_FAILED", {
        "service": service,
        "error": "registry unreachable after all retries",
        "latency_ms": round(latency_ms, 2),
        "attempts": retries,
    })
    return {
        "ok": False,
        "error": "registry unreachable",
        "servers": [],
        "latency_ms": round(latency_ms, 2),
        "attempts": retries,
    }


def run_connect(ip: str, port: int, message: str = "hello",
                timeout: float = 2.0, retries: int = 2) -> dict:
    """
    Send a service request (UDP) to a server and wait for its reply.
    Events fired:
      CLIENT / CONNECT_STARTED
      CLIENT / REQUEST_SENT         (once per attempt)
      CLIENT / REQUEST_TIMEOUT      (on per-attempt timeout)
      SERVER / RESPONSE_RECEIVED    (when reply arrives)
      CLIENT / CONNECT_SUCCESS      (success)   OR
      CLIENT / CONNECT_FAILED       (all retries exhausted)
    """
    event_bus.emit("CLIENT", "CONNECT_STARTED", {
        "target": f"{ip}:{port}",
        "message": message,
        "timeout_s": timeout,
        "max_retries": retries,
    })

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
    except OSError as exc:
        event_bus.emit("CLIENT", "CONNECT_FAILED", {
            "target": f"{ip}:{port}",
            "error": f"socket setup failed: {type(exc).__name__}: {exc}",
            "latency_ms": 0.0,
            "attempts": 0,
        })
        return {"ok": False, "ip": ip, "port": port,
                "error": f"socket setup failed: {type(exc).__name__}: {exc}",
                "latency_ms": 0.0, "attempts": 0}

    t0 = time.perf_counter()
    last_error = None
    timeout_failures = 0
    socket_failures = 0

    try:
        for attempt in range(1, retries + 1):
            event_bus.emit("CLIENT", "REQUEST_SENT", {
                "target": f"{ip}:{port}",
                "message": message,
                "attempt": attempt,
                "of": retries,
            })
            try:
                sock.sendto(message.encode("utf-8"), (ip, port))
                data, _ = sock.recvfrom(MAX_DGRAM)
                reply = data.decode("utf-8", "replace")
                latency_ms = (time.perf_counter() - t0) * 1000
                event_bus.emit("SERVER", "RESPONSE_RECEIVED", {
                    "from": f"{ip}:{port}",
                    "reply": reply,
                    "latency_ms": round(latency_ms, 2),
                    "attempt": attempt,
                })
                event_bus.emit("CLIENT", "CONNECT_SUCCESS", {
                    "target": f"{ip}:{port}",
                    "reply": reply,
                    "latency_ms": round(latency_ms, 2),
                    "attempts": attempt,
                })
                return {
                    "ok": True,
                    "ip": ip,
                    "port": port,
                    "reply": reply,
                    "latency_ms": round(latency_ms, 2),
                    "attempts": attempt,
                    "previous_failures": timeout_failures + socket_failures,
                }
            except OSError as exc:
                # UDP sockets may receive ICMP errors asynchronously. Windows
                # exposes these as ConnectionResetError/ConnectionRefusedError;
                # Linux may report them through other OSError subclasses.
                # Count the failed attempt and continue using the same retry
                # schedule on every platform.
                last_error = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, (socket.timeout, TimeoutError)):
                    timeout_failures += 1
                    event_bus.emit("CLIENT", "REQUEST_TIMEOUT", {
                        "target": f"{ip}:{port}",
                        "attempt": attempt,
                        "of": retries,
                        "error": last_error,
                    })
                else:
                    socket_failures += 1
                    event_bus.emit("CLIENT", "REQUEST_FAILED", {
                        "target": f"{ip}:{port}",
                        "attempt": attempt,
                        "of": retries,
                        "error": last_error,
                    })
    finally:
        sock.close()

    latency_ms = (time.perf_counter() - t0) * 1000
    if last_error:
        final_error = f"all {retries} attempts failed; last socket error: {last_error}"
    else:
        final_error = "all retries timed out — server unreachable or SDN DENY active"
    event_bus.emit("CLIENT", "CONNECT_FAILED", {
        "target": f"{ip}:{port}",
        "error": final_error,
        "latency_ms": round(latency_ms, 2),
        "attempts": retries,
        "timeout_failures": timeout_failures,
        "socket_failures": socket_failures,
    })
    return {
        "ok": False,
        "ip": ip,
        "port": port,
        "error": final_error,
        "latency_ms": round(latency_ms, 2),
        "attempts": retries,
        "timeout_failures": timeout_failures,
        "socket_failures": socket_failures,
    }

