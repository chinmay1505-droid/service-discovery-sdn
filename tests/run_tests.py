#!/usr/bin/env python3
"""CLI Test Runner for Service Discovery SDN.

Executes the automated 17-point verification suite against the real networking system.
Usage:
  python3 tests/run_tests.py [--registry 127.0.0.1]
"""
import argparse
import os
import sys
import threading
import time

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_BACKEND = os.path.join(_ROOT, "backend")
_SRC = os.path.join(_ROOT, "src")

for p in (_BACKEND, _SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

from services.registry_bridge import BridgedRegistry
from services import test_runner


def main():
    parser = argparse.ArgumentParser(description="Service Discovery SDN Test Suite")
    parser.add_argument("--registry", default="127.0.0.1", help="Registry IP address")
    args = parser.parse_args()

    print("============================================================")
    print("      SERVICE DISCOVERY SDN — AUTOMATED VERIFICATION        ")
    print("============================================================")
    print(f"Target Registry: {args.registry}")

    # Check if a registry is already running, or start an embedded instance
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.2)
    already_running = False
    try:
        s.sendto(b'{"type":"DISCOVER","service":"*"}', (args.registry, 5001))
        # if send succeeds, test if reply comes
        s.recvfrom(2048)
        already_running = True
        reg = None
    except Exception:
        already_running = False
    finally:
        s.close()

    if not already_running and args.registry in ("127.0.0.1", "0.0.0.0"):
        print("[*] Starting embedded BridgedRegistry for test execution...")
        reg = BridgedRegistry(bind_ip="127.0.0.1")
        t = threading.Thread(target=reg.run, daemon=True)
        t.start()
        time.sleep(1.0)
    else:
        reg = None

    try:
        res = test_runner.run_all_tests(registry_ip=args.registry)
    finally:
        if reg:
            reg.running = False

    print("\n" + "=" * 60)
    executed = res["total"] - res.get("skipped", 0)
    print(f"TEST RESULTS: {res['passed']} passed, {res['failed']} failed, "
          f"{res.get('skipped', 0)} skipped ({res['score_pct']}% of {executed} executed)")
    print("=" * 60)
    for t in res["tests"]:
        badge = t.get("status", "PASS" if t["passed"] else "FAIL")
        print(f"[{badge}] {t['id']}: {t['name']}")
        print(f"       Spec:   {t['spec']}")
        print(f"       Detail: {t['detail']}")

    sys.exit(0 if res["passed"] == res["total"] else 1)


if __name__ == "__main__":
    main()
