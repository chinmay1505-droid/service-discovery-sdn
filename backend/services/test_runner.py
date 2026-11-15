"""Comprehensive Automated Verification Test Suite.

Validates the complete Service Discovery SDN system across 4 distinct domains:
  1. Networking (Sockets, Registry, Server, Client)
  2. SDN (OpenFlow 1.3, Controller, Priorities, Flow Lifecycle)
  3. Robustness (Expiry, Rogue Server, Retries, Deregister)
  4. Backend API & REST Integration
"""
import os
import socket
import time
import uuid
from typing import Dict, Any, List

from common import REGISTRY_ADVERT_PORT, REGISTRY_QUERY_PORT, SERVICE_PORT, encode
from services import event_bus
from services import process_manager
from services.client_runner import run_discover, run_connect
from services.flow_inspector import get_flows


def run_all_tests(registry_ip: str = "127.0.0.1") -> Dict[str, Any]:
    """Execute all 17 automated tests and return detailed results."""
    t0 = time.time()
    event_bus.emit("SYSTEM", "TEST_SUITE_STARTED", {"total": 17})

    tests: List[Dict[str, Any]] = []

    def add_test(tid: str, category: str, name: str, passed: bool | None, spec: str, detail: str):
        status = "SKIPPED" if passed is None else ("PASS" if passed else "FAIL")
        tests.append({
            "id": tid,
            "category": category,
            "name": name,
            "passed": passed,
            "status": status,
            "spec": spec,
            "detail": detail,
        })
        event_bus.emit("SYSTEM", "TEST_CASE_COMPLETED", {
            "id": tid,
            "name": name,
            "passed": passed,
            "status": status,
        })

    def wait_for_registered_server(name: str, service: str, wait_s: float = 3.0):
        """Return (registry_reachable, server_found, last_reply)."""
        deadline = time.monotonic() + wait_s
        registry_reachable = False
        last_reply = {"ok": False, "servers": [], "error": "no registry response"}
        while time.monotonic() < deadline:
            last_reply = run_discover(registry_ip, service, timeout=0.3, retries=1)
            if last_reply.get("ok"):
                registry_reachable = True
                if any(s.get("name") == name for s in last_reply.get("servers", [])):
                    return True, True, last_reply
            time.sleep(0.2)
        return registry_reachable, False, last_reply

    def wait_for_server_absence(name: str, service: str, wait_s: float = 2.0):
        """Return (registry_reachable, server_absent, last_reply)."""
        deadline = time.monotonic() + wait_s
        registry_reachable = False
        last_reply = {"ok": False, "servers": [], "error": "no registry response"}
        while time.monotonic() < deadline:
            last_reply = run_discover(registry_ip, service, timeout=0.3, retries=1)
            if last_reply.get("ok"):
                registry_reachable = True
                if not any(s.get("name") == name for s in last_reply.get("servers", [])):
                    return True, True, last_reply
            time.sleep(0.2)
        return registry_reachable, False, last_reply

    # =========================================================================
    # DOMAIN 1: NETWORKING (Low-Level Sockets & Protocol)
    # =========================================================================

    # T01: Verify advert path by observing a valid one-time registration.
    s1 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe_service = "__test_advert_probe__"
        s1.sendto(encode({"type": "ADVERTISE", "name": "test-advert-probe",
                          "service": probe_service, "port": 47001, "ttl": 2}),
                  (registry_ip, REGISTRY_ADVERT_PORT))
        probe_result = {"servers": []}
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            probe_result = run_discover(registry_ip, probe_service, timeout=0.15, retries=1)
            if any(item.get("name") == "test-advert-probe" for item in probe_result.get("servers", [])):
                break
        t1_ok = any(item.get("name") == "test-advert-probe" for item in probe_result.get("servers", []))
        t1_msg = "valid ADVERTISE observed in subsequent registry query" if t1_ok else "no matching registry entry observed"
        s1.sendto(encode({"type": "DEREGISTER", "name": "test-advert-probe",
                          "service": probe_service}), (registry_ip, REGISTRY_ADVERT_PORT))
    except Exception as exc:
        t1_ok = False
        t1_msg = str(exc)
    finally:
        s1.close()
    add_test("NET-01", "Networking", "Registry Advert Port Active", t1_ok,
             f"UDP {REGISTRY_ADVERT_PORT} bound and accepting datagrams", t1_msg)

    # T02: Query path must receive a valid protocol reply, not merely send UDP.
    s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        query_result = run_discover(registry_ip, "__test_query_probe__", timeout=0.4, retries=1)
        t2_ok = query_result.get("ok") is True and query_result.get("count") == 0
        t2_msg = "valid empty REPLY received" if t2_ok else str(query_result.get("error", "invalid query response"))
    except Exception as exc:
        t2_ok, t2_msg = False, str(exc)
    finally:
        s2.close()
    add_test("NET-02", "Networking", "Registry Query Port Active", t2_ok,
             f"UDP {REGISTRY_QUERY_PORT} bound and accepting queries", t2_msg)

    # T03: Service Server Startup & Advertisement
    srv_res = process_manager.start_server("test-srv1", "chat", registry_ip=registry_ip, port=7000, interval=1.0, ttl=5.0)
    time.sleep(1.5)  # Wait for first advert
    disc_chat = run_discover(registry_ip, "chat", timeout=0.5, retries=1)
    chat_names = [s["name"] for s in disc_chat.get("servers", [])]
    t3_ok = "test-srv1" in chat_names
    add_test("NET-03", "Networking", "Server UDP Advertisement", t3_ok,
             "Server sends UDP advertisement and registry records entry",
             f"Found servers: {chat_names}")

    # T04: Service Discovery Query Filtering
    disc_all = run_discover(registry_ip, "*", timeout=0.5, retries=1)
    all_names = [s["name"] for s in disc_all.get("servers", [])]
    t4_ok = disc_all.get("ok") and len(all_names) >= 1
    add_test("NET-04", "Networking", "Wildcard Service Discovery", t4_ok,
             "Client discovery with '*' returns all registered services",
             f"Discovered {len(all_names)} server(s): {all_names}")

    # T05: Unknown Service Query (Correct Negative Reply)
    disc_unk = run_discover(registry_ip, "quantum-teleportation", timeout=0.5, retries=1)
    t5_ok = disc_unk.get("ok") and disc_unk.get("count") == 0
    add_test("NET-05", "Networking", "Unknown Service Handling", t5_ok,
             "Query for unregistered service returns empty list (count=0)",
             f"count={disc_unk.get('count', 0)}, ok={disc_unk.get('ok')}")

    # T06: End-to-End Service Usage (UDP Echo)
    srv_endpoint = next((s for s in disc_chat.get("servers", []) if s.get("name") == "test-srv1"), None)
    conn_res = run_connect(srv_endpoint["ip"], int(srv_endpoint["port"]), message="verification-echo", timeout=1.0) if srv_endpoint else {"ok": False}
    t6_ok = conn_res.get("ok") and "test-srv1" in str(conn_res.get("reply"))
    add_test("NET-06", "Networking", "End-to-End Service Execution", t6_ok,
             "Client sends payload to discovered server and receives valid echo response",
             f"Response: '{conn_res.get('reply')}', latency={conn_res.get('latency_ms')}ms")

    # =========================================================================
    # DOMAIN 2: SDN & OPENFLOW CONTROLLER
    # =========================================================================
    flows_data = get_flows()
    flow_list = flows_data.get("flows", [])
    priorities = {f["priority"] for f in flow_list}

    # T07: Priority 0 Table-Miss Rule
    t7_ok = (0 in priorities) if flows_data.get("available") else None
    add_test("SDN-01", "SDN / OpenFlow", "Table-Miss Rule (Priority 0)", t7_ok,
             "Default OpenFlow 1.3 table-miss rule redirects unknown traffic to controller",
             "Verified in live ovs-ofctl output" if t7_ok else ("Live switch query unavailable" if t7_ok is None else "Priority 0 rule missing from live switch"))

    # T08: Priority 250 Advertisement Interception
    t8_ok = (250 in priorities) if flows_data.get("available") else None
    add_test("SDN-02", "SDN / OpenFlow", "Advertisement Snooping (Priority 250)", t8_ok,
             "Snoop rule intercepts UDP port 5000 datagrams for controller awareness",
             "Verified in live ovs-ofctl output" if t8_ok else ("Live switch query unavailable" if t8_ok is None else "Priority 250 rule missing from live switch"))

    # T09: Priority 200 Default-Deny Policy
    t9_ok = (200 in priorities) if flows_data.get("available") else None
    add_test("SDN-03", "SDN / OpenFlow", "Default-Deny Policy (Priority 200)", t9_ok,
             "Zero-trust drop rule blocks all unauthorized UDP port 7000 traffic",
             "Verified in live ovs-ofctl output" if t9_ok else ("Live switch query unavailable" if t9_ok is None else "Priority 200 rule missing from live switch"))

    # T10: Priority 300 Dynamic ALLOW Flow
    t10_ok = (bool(srv_endpoint and any(f.get("priority") == 300 and srv_endpoint["ip"] in f.get("match", "")
                                        for f in flow_list))) if flows_data.get("available") else None
    add_test("SDN-04", "SDN / OpenFlow", "Dynamic ALLOW Flow (Priority 300)", t10_ok,
             "Dynamic flow installed for advertised server beating default-deny rule",
             "Verified in live ovs-ofctl output" if t10_ok else ("Live switch query unavailable" if t10_ok is None else "Priority 300 ALLOW rule missing from live switch"))

    # =========================================================================
    # DOMAIN 3: ROBUSTNESS & FAILURE HANDLING
    # =========================================================================

    # T11: Clean Server Deregistration. Require a real registration before stop.
    clean_id = uuid.uuid4().hex
    clean_name = f"rob01-{clean_id}"
    clean_service = f"rob01-{clean_id}"
    start_clean = process_manager.start_server(clean_name, clean_service, registry_ip=registry_ip,
                                               port=7000, interval=1.0, ttl=5.0)
    reg_reachable, registered, pre_stop = wait_for_registered_server(clean_name, clean_service) if start_clean.get("ok") else (False, False, start_clean)
    if not start_clean.get("ok"):
        t11_status, t11_detail = False, f"Server did not start: {start_clean.get('error')}"
    elif not reg_reachable:
        process_manager.stop_server(clean_name, hard=True)
        t11_status, t11_detail = None, f"Skipped: registry query unavailable before deregistration: {pre_stop.get('error')}"
    elif not registered:
        process_manager.stop_server(clean_name, hard=True)
        t11_status, t11_detail = False, "Server started but was not discoverable before stop"
    else:
        stop_clean = process_manager.stop_server(clean_name, hard=False)
        reachable_after, absent_after, post_stop = wait_for_server_absence(clean_name, clean_service) if stop_clean.get("ok") else (True, False, stop_clean)
        if not stop_clean.get("ok"):
            t11_status, t11_detail = False, f"Graceful stop failed: {stop_clean.get('error')}"
        elif not reachable_after:
            t11_status, t11_detail = None, f"Skipped: registry query unavailable after stop: {post_stop.get('error')}"
        else:
            t11_status = absent_after
            t11_detail = "Server was registered before stop and absent after successful registry queries" if absent_after else "Server remained discoverable after graceful stop"
    add_test("ROB-01", "Robustness", "Clean Deregistration (SIGTERM)", t11_status,
             "Verify registration before SIGTERM, then verify removal from a successful registry query",
             t11_detail)

    # T12: Crash and TTL expiration. Do not interpret a missing initial entry as expiry.
    crash_id = uuid.uuid4().hex
    crash_name = f"rob02-{crash_id}"
    crash_service = f"rob02-{crash_id}"
    start_crash = process_manager.start_server(crash_name, crash_service, registry_ip=registry_ip,
                                               port=7000, interval=1.0, ttl=2.0)
    reg_reachable, registered, pre_crash = wait_for_registered_server(crash_name, crash_service) if start_crash.get("ok") else (False, False, start_crash)
    if not start_crash.get("ok"):
        t12_status, t12_detail = False, f"Crash-test server did not start: {start_crash.get('error')}"
    elif not reg_reachable:
        process_manager.stop_server(crash_name, hard=True)
        t12_status, t12_detail = None, f"Skipped: registry query unavailable before crash: {pre_crash.get('error')}"
    elif not registered:
        process_manager.stop_server(crash_name, hard=True)
        t12_status, t12_detail = False, "Crash-test server started but was not discoverable before crash"
    else:
        crash_result = process_manager.stop_server(crash_name, hard=True)
        if not crash_result.get("ok"):
            t12_status, t12_detail = False, f"Could not inject crash: {crash_result.get('error')}"
        else:
            time.sleep(3.5)  # configured TTL is 2s; registry reaper checks once per second
            reachable_after, absent_after, post_crash = wait_for_server_absence(crash_name, crash_service)
            if not reachable_after:
                t12_status, t12_detail = None, f"Skipped: registry query unavailable after crash: {post_crash.get('error')}"
            else:
                t12_status = absent_after
                t12_detail = "Server was registered before crash and absent after TTL on successful registry queries" if absent_after else "Server remained discoverable after TTL wait"
    add_test("ROB-02", "Robustness", "Server Crash & TTL Expiration", t12_status,
             "Verify registration before hard crash, wait beyond TTL, then verify removal",
             t12_detail)

    # T13: Establish a working registry query before and after launching rogue host.
    baseline = run_discover(registry_ip, "chat", timeout=0.5, retries=1)
    if not baseline.get("ok"):
        t13_status, t13_detail = None, f"Skipped: baseline registry query failed: {baseline.get('error')}"
    else:
        rogue_start = process_manager.start_server("test-rogue", "chat", registry_ip=registry_ip,
                                                   port=7000, no_advertise=True)
        if not rogue_start.get("ok"):
            t13_status, t13_detail = False, f"Rogue test process did not start: {rogue_start.get('error')}"
        else:
            time.sleep(0.5)
            after_rogue = run_discover(registry_ip, "chat", timeout=0.5, retries=1)
            rogue_running = any(s.get("name") == "test-rogue" and s.get("running")
                                for s in process_manager.get_managed_servers())
            if not after_rogue.get("ok"):
                t13_status, t13_detail = None, f"Skipped: registry query failed after rogue start: {after_rogue.get('error')}"
            else:
                rogue_absent = not any(s.get("name") == "test-rogue" for s in after_rogue.get("servers", []))
                t13_status = bool(rogue_running and rogue_absent)
                t13_detail = f"Baseline and follow-up registry queries succeeded; process running={rogue_running}; rogue absent={rogue_absent}; packet-drop enforcement requires Mininet"
            process_manager.stop_server("test-rogue", hard=True)
    add_test("ROB-03", "Robustness", "Unadvertised Server Not Registered", t13_status,
             "Require successful registry queries before/after launch and verify running rogue server is absent",
             t13_detail)

    # T14: Client UDP Retransmission Logic
    # Exercise the real retry loop against a local UDP port with no listener.
    sock_test = None
    try:
        sock_test = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock_test.bind(("127.0.0.1", 0))
        closed_port = sock_test.getsockname()[1]
    except OSError as exc:
        closed_port = None
        setup_error = f"{type(exc).__name__}: {exc}"
    finally:
        if sock_test is not None:
            sock_test.close()

    if closed_port is None:
        add_test("ROB-04", "Robustness", "Client Retransmission & Timeout", None,
                 "Client performs 3 retransmission attempts before declaring failure",
                 f"Skipped: could not reserve a local UDP port: {setup_error}")
    else:
        t0_retries = time.perf_counter()
        try:
            retry_result = run_connect("127.0.0.1", closed_port, "retry-probe", timeout=0.1, retries=3)
            retries_count = retry_result.get("attempts", 0)
            t14_ok = not retry_result.get("ok") and retries_count == 3
            retry_detail = (f"Real UDP retry loop made {retries_count} attempts; "
                            f"timeouts={retry_result.get('timeout_failures', 0)}, "
                            f"socket errors={retry_result.get('socket_failures', 0)}; "
                            f"elapsed={round((time.perf_counter() - t0_retries)*1000, 1)}ms")
        except OSError as exc:
            # The suite must retain a result even if the platform rejects a
            # socket operation outside run_connect's retry loop.
            t14_ok = False
            retry_detail = f"Retry test failed with {type(exc).__name__}: {exc}"
        except Exception as exc:
            t14_ok = False
            retry_detail = f"Retry test failed unexpectedly with {type(exc).__name__}: {exc}"
        add_test("ROB-04", "Robustness", "Client Retransmission & Timeout", t14_ok,
                 "Client performs 3 retransmission attempts before declaring failure",
                 retry_detail)

    # =========================================================================
    # DOMAIN 4: BACKEND API INTEGRATION
    # =========================================================================

    # These checks need an actual ASGI HTTP/WebSocket client. Service-function
    # calls above are not equivalent to exercising the route contract.
    add_test("API-01", "Backend API", "Discovery REST Endpoint", None,
             "POST /api/discover must be exercised through an HTTP test client",
             "Skipped: this runner has no ASGI client/lifespan isolation; direct service calls are not counted as route tests")
    add_test("API-02", "Backend API", "Connect REST Endpoint", None,
             "POST /api/connect must be exercised through an HTTP test client",
             "Skipped: this runner has no ASGI client/lifespan isolation")
    add_test("API-03", "Backend API", "WebSocket Event Delivery", None,
             "A WebSocket client must receive an emitted event",
             "Skipped: event-bus emission alone does not verify WebSocket delivery")

    # =========================================================================
    # SUMMARY COMPUTATION
    # =========================================================================
    total = len(tests)
    passed = sum(1 for t in tests if t["status"] == "PASS")
    failed = sum(1 for t in tests if t["status"] == "FAIL")
    skipped = sum(1 for t in tests if t["status"] == "SKIPPED")
    score_pct = round((passed / (total - skipped)) * 100, 1) if total > skipped else 0.0

    event_bus.emit("SYSTEM", "TEST_SUITE_COMPLETED", {
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "score_pct": score_pct,
        "elapsed_s": round(time.time() - t0, 2),
    })

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "score_pct": score_pct,
        "elapsed_s": round(time.time() - t0, 2),
        "tests": tests,
    }
