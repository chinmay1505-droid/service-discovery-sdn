"""Repeatable socket measurements for the service discovery application.

Only successful UDP exchanges contribute latency samples. Failed attempts are
counted separately, and every run is saved as timestamped raw JSON evidence.
"""
import json
import math
import os
import statistics
import time
from datetime import datetime, timezone
from typing import Any

from services import event_bus
from services.client_runner import run_discover, run_connect

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_EVIDENCE = os.path.join(_ROOT, "docs", "evidence")


def _stats(samples: list[float], attempted: int) -> dict[str, Any]:
    if not samples:
        return {"count": 0, "attempted": attempted, "failed": attempted,
                "success_rate_pct": 0.0, "min": None, "max": None, "mean": None,
                "median": None, "std": None, "p95": None}
    ordered = sorted(samples)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    return {
        "count": len(samples), "attempted": attempted, "failed": attempted - len(samples),
        "success_rate_pct": round(100 * len(samples) / attempted, 2) if attempted else 0.0,
        "min": round(min(samples), 3), "max": round(max(samples), 3),
        "mean": round(statistics.fmean(samples), 3),
        "median": round(statistics.median(samples), 3),
        "std": round(statistics.pstdev(samples), 3) if len(samples) > 1 else 0.0,
        "p95": round(p95, 3),
    }


def run_benchmark(registry_ip: str = "127.0.0.1", trials: int = 10) -> dict[str, Any]:
    """Measure discovery, invocation, and combined lookup plus invocation."""
    event_bus.emit("PERF", "BENCHMARK_STARTED", {"trials": trials, "registry_ip": registry_ip})
    raw: list[dict[str, Any]] = []
    discovery_ms: list[float] = []
    communication_ms: list[float] = []
    end_to_end_ms: list[float] = []
    d_ok = c_ok = e_ok = 0

    for i in range(trials):
        started = time.perf_counter()
        discovery = run_discover(registry_ip, "chat", timeout=0.5, retries=1)
        measured_discovery = (time.perf_counter() - started) * 1000
        if discovery.get("ok") and discovery.get("servers"):
            d_ok += 1
            discovery_ms.append(measured_discovery)
            endpoint = discovery["servers"][0]
            started = time.perf_counter()
            connect = run_connect(endpoint["ip"], int(endpoint["port"]),
                                  message=f"bench-{i}", timeout=0.5, retries=1)
            measured_connect = (time.perf_counter() - started) * 1000
            if connect.get("ok"):
                c_ok += 1
                communication_ms.append(measured_connect)
                end_to_end_ms.append(measured_discovery + measured_connect)
                e_ok += 1
            raw.append({"trial": i + 1, "discovery_ok": True,
                        "discovery_latency_ms": round(measured_discovery, 3),
                        "endpoint": f"{endpoint['ip']}:{endpoint['port']}",
                        "communication_ok": bool(connect.get("ok")),
                        "communication_latency_ms": round(measured_connect, 3),
                        "end_to_end_latency_ms": round(measured_discovery + measured_connect, 3)
                        if connect.get("ok") else None})
        else:
            raw.append({"trial": i + 1, "discovery_ok": False,
                        "discovery_latency_ms": round(measured_discovery, 3),
                        "endpoint": None, "communication_ok": False,
                        "communication_latency_ms": None, "end_to_end_latency_ms": None,
                        "error": discovery.get("error", "no matching service endpoint")})
        time.sleep(0.01)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {"registry_ip": registry_ip, "service": "chat", "transport": "UDP",
                        "trials_requested": trials, "baseline": "not measured; provide a configured static endpoint to compare"},
        "discovery_latency_ms": _stats(discovery_ms, trials),
        "communication_latency_ms": _stats(communication_ms, d_ok),
        "end_to_end_latency_ms": _stats(end_to_end_ms, trials),
        "trials_raw": raw,
    }
    os.makedirs(_EVIDENCE, exist_ok=True)
    path = os.path.join(_EVIDENCE, f"benchmark-{timestamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    result["evidence_file"] = os.path.relpath(path, _ROOT).replace(os.sep, "/")
    event_bus.emit("PERF", "BENCHMARK_COMPLETED", {
        "discovery_successes": d_ok, "communication_successes": c_ok,
        "end_to_end_successes": e_ok, "evidence_file": result["evidence_file"]})
    return result


def get_baseline_comparison() -> dict[str, Any]:
    """Describe architectural differences without inventing measurements."""
    return {
        "metrics": [
            {"category": "Service discovery", "static_baseline": "Configured IP/port lookup (no registry round trip)",
             "dynamic_sdn": "UDP advertisement and registry query on ports 5000/5001",
             "advantage": "Dynamic endpoint updates; static baseline has less lookup work"},
            {"category": "Traffic authorization", "static_baseline": "Requires manually maintained firewall/ACL entries",
             "dynamic_sdn": "Priority 200 default deny with dynamic priority 300 allow rules",
             "advantage": "Authorization follows advertised service state when the live SDN mode is running"},
            {"category": "Failure handling", "static_baseline": "Endpoint updates are manual unless paired with another discovery mechanism",
             "dynamic_sdn": "Registry TTL expiry and controller flow revocation",
             "advantage": "Automated removal is available; actual convergence must be measured in Mininet"},
        ],
        "comparison_status": "qualitative_only",
        "summary": "No static-baseline latency or recovery measurements are claimed. Run comparable endpoint trials in the target environment before making quantitative claims.",
    }
