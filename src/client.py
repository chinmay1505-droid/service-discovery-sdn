#!/usr/bin/env python3
"""Discovery client.

  client.py --registry 10.0.0.100 --service chat            # discover
  client.py --registry 10.0.0.100 --service chat --use      # discover, then call the first server
  client.py --direct 10.0.0.12:7000 --message hi            # call a server directly (no discovery)

UDP is unreliable, so a query is retransmitted if no reply arrives in time.
Discovery latency = time from the first DISCOVER sent to the matching REPLY received.
"""
import argparse
import json
import socket
import sys
import time
import uuid

from common import REGISTRY_QUERY_PORT, MAX_DGRAM, encode, decode


def discover(registry, service, timeout=1.0, retries=3):
    """Return (servers, latency_ms, attempts). servers is None if registry never answered."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    req_id = uuid.uuid4().hex[:8]
    query = encode({"type": "DISCOVER", "service": service, "req_id": req_id})
    t0 = time.perf_counter()
    for attempt in range(1, retries + 1):
        sock.sendto(query, (registry, REGISTRY_QUERY_PORT))
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            sock.settimeout(max(0.01, deadline - time.perf_counter()))
            try:
                data, _ = sock.recvfrom(MAX_DGRAM)
            except socket.timeout:
                break
            msg = decode(data)
            if msg and msg["type"] == "REPLY" and msg.get("req_id") == req_id:
                return msg["servers"], (time.perf_counter() - t0) * 1000, attempt
    return None, (time.perf_counter() - t0) * 1000, retries


def call_service(ip, port, text, timeout=2.0, retries=2):
    """Send one datagram to a service and wait for its answer. Returns reply text or None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    for _ in range(retries):
        sock.sendto(text.encode(), (ip, port))
        try:
            data, _ = sock.recvfrom(MAX_DGRAM)
            return data.decode("utf-8", "replace")
        except socket.timeout:
            continue
    return None


def main():
    ap = argparse.ArgumentParser(description="Service discovery client")
    ap.add_argument("--registry")
    ap.add_argument("--service", default="*", help="service type, or * for all")
    ap.add_argument("--use", action="store_true", help="call the first discovered server")
    ap.add_argument("--direct", help="IP:PORT of a server to call without discovery")
    ap.add_argument("--message", default="hello")
    ap.add_argument("--json", action="store_true", help="print one JSON result line")
    a = ap.parse_args()
    result = {"ok": False}

    if a.direct:
        ip, port = a.direct.split(":")
        reply = call_service(ip, int(port), a.message)
        result.update(mode="direct", target=a.direct, reply=reply, ok=reply is not None)
        code = 0 if reply else 4
    else:
        if not a.registry:
            ap.error("--registry is required unless --direct is used")
        servers, ms, attempts = discover(a.registry, a.service)
        if servers is None:
            result.update(mode="discover", error="registry unreachable", latency_ms=round(ms, 2))
            code = 3
        else:
            result.update(mode="discover", service=a.service, servers=servers,
                          count=len(servers), latency_ms=round(ms, 2), attempts=attempts, ok=True)
            code = 0 if servers else 2
            if a.use and servers:
                s = servers[0]
                reply = call_service(s["ip"], s["port"], a.message)
                result.update(used=s["name"], reply=reply, ok=reply is not None)
                code = 0 if reply else 4

    if a.json:
        print(json.dumps(result))
    else:
        if result.get("mode") == "direct":
            print("Direct call to %s -> %s" % (a.direct, result["reply"] or "NO REPLY (timeout)"))
        elif "error" in result:
            print("ERROR: %s (after %.0f ms)" % (result["error"], result["latency_ms"]))
        else:
            print("Discovery of '%s': %d server(s) found in %.2f ms (attempts: %d)"
                  % (a.service, result["count"], result["latency_ms"], result["attempts"]))
            for s in result["servers"]:
                print("   - %-6s %-6s %s:%d  (last advert %.1fs ago)"
                      % (s["name"], s["service"], s["ip"], s["port"], s["age"]))
            if "used" in result:
                print("Used %s -> %s" % (result["used"], result["reply"] or "NO REPLY (timeout)"))
    sys.exit(code)


if __name__ == "__main__":
    main()
