"""Robustness Lab test executor.

Implements real executions of the 4 core robustness scenarios:
  Scenario A: Server Crash & TTL Expiration & Flow Revocation
  Scenario B: Rogue Server & Default-Deny Blocking
  Scenario C: Unknown Service Lookup
  Scenario D: UDP Retransmission & Packet Loss Handling
"""
import socket
import threading
import time
from typing import Dict, Any

from services import event_bus
from services import process_manager
from services.client_runner import run_discover, run_connect
from services.flow_inspector import get_flows


def run_scenario_a(registry_ip: str = "127.0.0.1", server_name: str = "srv1") -> Dict[str, Any]:
    """
    Scenario A — Server Crash:
    Starts server if needed -> Hard kills server process (SIGKILL) ->
    Observes silence -> Registry expires entry -> SDN flow revoked.
    """
    steps = []
    t0 = time.time()

    # Step 1: Ensure server is running and advertised
    steps.append({"step": 1, "title": "Check / Start Server", "status": "IN_PROGRESS"})
    event_bus.emit("ROBUSTNESS", "SCENARIO_A_STARTED", {"server": server_name})

    res = process_manager.start_server(name=server_name, service="chat", registry_ip=registry_ip, ttl=3.0, interval=1.0)
    time.sleep(1.5)  # Wait for first advert
    before = run_discover(registry_ip, "chat", timeout=0.4, retries=2)
    registered = res.get("ok") and any(s.get("name") == server_name for s in before.get("servers", []))
    steps[0]["status"] = "PASS" if registered else "FAIL"
    steps[0]["detail"] = f"Start result={res.get('ok')}; advertised entry observed={bool(registered)}"

    # Step 2: Test service before crash
    steps.append({"step": 2, "title": "Verify Service Reachability", "status": "IN_PROGRESS"})
    endpoint = next((s for s in before.get("servers", []) if s.get("name") == server_name), None)
    conn1 = run_connect(ip=endpoint["ip"], port=int(endpoint["port"]), message="pre-crash-check", timeout=1.0) if endpoint else {"ok": False}
    if conn1.get("ok"):
        steps[1]["status"] = "PASS"
        steps[1]["detail"] = f"Service responded: '{conn1.get('reply')}'"
    else:
        steps[1]["status"] = "FAIL"
        steps[1]["detail"] = "Pre-crash UDP service call failed; crash lifecycle is not established"

    flows_before = get_flows()
    before_allow = any(f.get("priority") == 300 and endpoint and endpoint["ip"] in f.get("match", "")
                       for f in flows_before.get("flows", []))

    # Step 3: Hard kill the server (SIGKILL, no DEREGISTER sent)
    steps.append({"step": 3, "title": "Simulate Server Crash", "status": "IN_PROGRESS"})
    kill_res = process_manager.stop_server(server_name, hard=True)
    event_bus.emit("ROBUSTNESS", "SERVER_CRASH_INJECTED", {
        "server": server_name,
        "mode": "SIGKILL (unclean crash, no DEREGISTER)",
    })
    steps[2]["status"] = "PASS" if kill_res.get("ok") else "FAIL"
    steps[2]["detail"] = f"Hard-stop result={kill_res.get('ok')}: {kill_res.get('message', kill_res.get('error'))}"

    # Step 4: Wait for TTL expiry
    steps.append({"step": 4, "title": "Heartbeat Silence & TTL Expiry", "status": "IN_PROGRESS"})
    time.sleep(3.5)  # Wait for 3.0s TTL to expire in registry reaper
    steps[3]["status"] = "IN_PROGRESS"
    steps[3]["detail"] = "Waiting for and checking actual registry TTL expiry"

    # Step 5: Verify service is no longer in registry
    steps.append({"step": 5, "title": "Verify Registry Removal & Traffic Block", "status": "IN_PROGRESS"})
    disc = run_discover(registry_ip, "chat", timeout=0.5, retries=1)
    found_names = [s["name"] for s in disc.get("servers", [])]
    is_gone = server_name not in found_names
    steps[3]["status"] = "PASS" if is_gone else "FAIL"
    steps[4]["status"] = "PASS" if is_gone else "FAIL"
    steps[4]["detail"] = f"Discovered servers: {found_names}. '{server_name}' successfully removed."
    flows_after = get_flows()
    if flows_before.get("available") and flows_after.get("available"):
        after_allow = any(f.get("priority") == 300 and endpoint and endpoint["ip"] in f.get("match", "")
                          for f in flows_after.get("flows", []))
        flow_status = "PASS" if before_allow and not after_allow else "FAIL"
        flow_detail = f"Actual OpenFlow ALLOW before={before_allow}, after={after_allow}"
    else:
        flow_status = "SKIPPED"
        flow_detail = "Live ovs-ofctl unavailable; flow revocation was not verified"
    steps.append({"step": 6, "title": "Verify OpenFlow ALLOW Removal", "status": flow_status,
                  "detail": flow_detail})

    event_bus.emit("ROBUSTNESS", "SCENARIO_A_COMPLETED", {
        "server": server_name,
        "elapsed_s": round(time.time() - t0, 2),
        "removed": is_gone,
    })

    return {
        "scenario": "A",
        "title": "Server Crash & TTL Expiration",
        "ok": bool(is_gone and all(s["status"] == "PASS" for s in steps)),
        "steps": steps,
        "elapsed_s": round(time.time() - t0, 2),
    }


def run_scenario_b(registry_ip: str = "127.0.0.1") -> Dict[str, Any]:
    """
    Scenario B — Rogue Server:
    Starts a server with --no-advertise on port 7000 ->
    Attacks / attempts to use service ->
    SDN Default Deny drops the traffic.
    """
    steps = []
    t0 = time.time()
    event_bus.emit("ROBUSTNESS", "SCENARIO_B_STARTED", {"target": "rogue"})

    steps = [{"step": 1, "title": "Verify Rogue Packet Drop", "status": "SKIPPED",
              "detail": "This runner starts host processes and cannot place a rogue server inside a Mininet namespace. Use the Mininet topology scenario for actual packet-drop evidence."}]

    event_bus.emit("ROBUSTNESS", "SCENARIO_B_COMPLETED", {
        "elapsed_s": round(time.time() - t0, 2),
        "policy": "NOT_VERIFIED",
    })

    return {
        "scenario": "B",
        "title": "Rogue Server & Default Deny",
        "ok": False,
        "steps": steps,
        "elapsed_s": round(time.time() - t0, 2),
    }


def run_scenario_c(registry_ip: str = "127.0.0.1", unknown_service: str = "printer") -> Dict[str, Any]:
    """
    Scenario C — Unknown Service:
    Queries registry for a service that has never been registered.
    Expects ok=True, count=0, servers=[].
    """
    t0 = time.time()
    event_bus.emit("ROBUSTNESS", "SCENARIO_C_STARTED", {"service": unknown_service})

    steps = [
        {"step": 1, "title": f"Query Registry for '{unknown_service}'", "status": "IN_PROGRESS"},
    ]

    res = run_discover(registry_ip, unknown_service, timeout=1.0, retries=2)
    ok = res.get("ok") and res.get("count") == 0

    steps[0]["status"] = "PASS" if ok else "FAIL"
    steps[0]["detail"] = (
        f"Registry returned: count={res.get('count', 0)}, "
        f"servers={res.get('servers', [])}, latency={res.get('latency_ms', 0)}ms"
    )

    event_bus.emit("ROBUSTNESS", "SCENARIO_C_COMPLETED", {
        "service": unknown_service,
        "count": res.get("count", 0),
        "ok": ok,
    })

    return {
        "scenario": "C",
        "title": "Unknown Service Discovery",
        "ok": ok,
        "steps": steps,
        "elapsed_s": round(time.time() - t0, 2),
    }


def run_scenario_d(registry_ip: str = "127.0.0.1") -> Dict[str, Any]:
    """
    Scenario D — UDP Packet Loss & Retransmission:
    Tests client retransmission logic by targeting an unresponsive UDP query port,
    demonstrating exponential or linear retry deadlines and timeout handling.
    """
    t0 = time.time()
    event_bus.emit("ROBUSTNESS", "SCENARIO_D_STARTED", {"retries": 3})

    steps = [
        {"step": 1, "title": "Start Local UDP Fault Receiver", "status": "IN_PROGRESS"},
        {"step": 2, "title": "Drop Initial Datagrams", "status": "IN_PROGRESS"},
        {"step": 3, "title": "Verify Real Client Retry and Recovery", "status": "IN_PROGRESS"},
    ]

    # This controlled endpoint drops the first two real datagrams and replies to
    # the third. run_connect performs the actual retransmission loop.
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(2.0)
    target_port = receiver.getsockname()[1]
    received = []
    def fault_receiver():
        try:
            while len(received) < 3:
                payload, peer = receiver.recvfrom(2048)
                received.append(payload.decode("utf-8", "replace"))
                if len(received) == 3:
                    receiver.sendto(b"recovered-after-loss", peer)
        except OSError:
            pass
        finally:
            receiver.close()

    worker = threading.Thread(target=fault_receiver, daemon=True)
    worker.start()
    steps[0]["status"] = "PASS"
    steps[0]["detail"] = f"Bound local UDP fault receiver on 127.0.0.1:{target_port}"
    t_start = time.perf_counter()
    result = run_connect("127.0.0.1", target_port, message="loss-probe", timeout=0.15, retries=3)
    worker.join(timeout=1.0)
    elapsed_ms = (time.perf_counter() - t_start) * 1000
    actual_retries = max(0, len(received) - 1)
    recovered = result.get("ok") and result.get("reply") == "recovered-after-loss"
    steps[1]["status"] = "PASS" if len(received) >= 3 else "SKIPPED"
    steps[1]["detail"] = (f"Receiver intentionally discarded datagrams 1 and 2; received {len(received)} attempts"
                           if received else "No datagrams reached the local receiver; loopback restrictions prevent verification")
    steps[2]["status"] = ("PASS" if recovered and result.get("attempts") == 3
                           else ("SKIPPED" if not received else "FAIL"))
    steps[2]["detail"] = f"Actual client attempts={result.get('attempts')}; recovery={bool(recovered)}; elapsed={round(elapsed_ms, 1)}ms"

    event_bus.emit("ROBUSTNESS", "SCENARIO_D_COMPLETED", {
        "attempts": result.get("attempts", 0),
        "received_datagrams": len(received),
        "dropped_datagrams": min(2, len(received)),
        "recovered": bool(recovered),
        "latency_ms": round(elapsed_ms, 2),
    })

    return {
        "scenario": "D",
        "title": "UDP Packet Loss & Retransmission",
        "ok": bool(recovered and len(received) == 3),
        "steps": steps,
        "attempts": result.get("attempts", 0),
        "received_datagrams": len(received),
        "recovered": bool(recovered),
        "elapsed_s": round(time.time() - t0, 2),
    }
