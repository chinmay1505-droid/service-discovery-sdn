#!/usr/bin/env python3
"""Service registry: the 'phone book' of the discovery system.

Receives UDP advertisements from servers, keeps a table of live services,
expires entries that go silent, and answers discovery queries from clients.

Threads (concurrency):
  1. advert thread : listens on UDP 5000 for ADVERTISE / DEREGISTER
  2. query thread  : listens on UDP 5001 for DISCOVER and sends REPLY
  3. reaper thread : removes entries whose TTL has expired
A lock protects the shared table.
"""
import argparse
import socket
import threading
import time

from common import (REGISTRY_ADVERT_PORT, REGISTRY_QUERY_PORT, SERVICE_TTL,
                    MAX_DGRAM, encode, decode, get_logger)


class Registry:
    def __init__(self, bind_ip="0.0.0.0", default_ttl=SERVICE_TTL, logfile=None):
        self.bind_ip = bind_ip
        self.default_ttl = default_ttl
        self.table = {}                 # (ip, port, service) -> entry dict
        self.lock = threading.Lock()
        self.running = True
        self.log = get_logger("registry", logfile)

    # ---------- table operations ----------
    def register(self, ip, msg):
        try:
            name, service, port = str(msg["name"]), str(msg["service"]), int(msg["port"])
            ttl = float(msg.get("ttl", self.default_ttl))
        except (KeyError, TypeError, ValueError):
            self.log.info("IGNORED malformed ADVERTISE from %s: %r", ip, msg)
            return
        if not (1 <= port <= 65535) or not (1 <= ttl <= 300):
            self.log.info("IGNORED out-of-range ADVERTISE from %s: %r", ip, msg)
            return
        key = (ip, port, service)
        now = time.time()
        with self.lock:
            new = key not in self.table
            entry = self.table.setdefault(key, {"adverts": 0, "first_seen": now})
            entry.update(name=name, service=service, ip=ip, port=port,
                         ttl=ttl, last_seen=now)
            entry["adverts"] += 1
        if new:
            self.log.info("REGISTERED   %s (%s) at %s:%d  ttl=%.0fs", name, service, ip, port, ttl)
            self.dump()

    def deregister(self, ip, msg):
        removed = []
        with self.lock:
            for key in [k for k in self.table if k[0] == ip and
                        (msg.get("service") in (None, k[2]))]:
                removed.append(self.table.pop(key))
        for e in removed:
            self.log.info("DEREGISTERED %s (%s) at %s:%d (clean shutdown)",
                          e["name"], e["service"], e["ip"], e["port"])
        if removed:
            self.dump()

    def lookup(self, service):
        now = time.time()
        with self.lock:
            hits = [e for e in self.table.values()
                    if service in ("*", e["service"])]
            result = [{"name": e["name"], "service": e["service"], "ip": e["ip"],
                       "port": e["port"], "age": round(now - e["last_seen"], 2)}
                      for e in hits]
        return sorted(result, key=lambda r: r["age"])      # freshest first

    def dump(self):
        with self.lock:
            rows = sorted("%s/%s@%s:%d" % (e["name"], e["service"], e["ip"], e["port"])
                          for e in self.table.values())
        self.log.info("TABLE (%d): %s", len(rows), ", ".join(rows) if rows else "<empty>")

    # ---------- threads ----------
    def advert_loop(self):
        sock = self._bind(REGISTRY_ADVERT_PORT)
        self.log.info("listening for advertisements on UDP %d", REGISTRY_ADVERT_PORT)
        while self.running:
            try:
                data, (ip, _sport) = sock.recvfrom(MAX_DGRAM)
            except socket.timeout:
                continue
            except OSError as exc:
                self.log.info("advert socket error: %s", exc)
                continue
            msg = decode(data)
            if msg is None:
                self.log.info("IGNORED malformed datagram from %s", ip)
            elif msg["type"] == "ADVERTISE":
                self.register(ip, msg)
            elif msg["type"] == "DEREGISTER":
                self.deregister(ip, msg)
            else:
                self.log.info("IGNORED unknown type %r from %s", msg["type"], ip)

    def query_loop(self):
        sock = self._bind(REGISTRY_QUERY_PORT)
        self.log.info("listening for discovery queries on UDP %d", REGISTRY_QUERY_PORT)
        while self.running:
            try:
                data, addr = sock.recvfrom(MAX_DGRAM)
            except socket.timeout:
                continue
            except OSError as exc:
                self.log.info("query socket error: %s", exc)
                continue
            msg = decode(data)
            if msg is None or msg["type"] != "DISCOVER":
                self.log.info("IGNORED bad query from %s", addr[0])
                continue
            service = str(msg.get("service", "*"))
            servers = self.lookup(service)
            reply = {"type": "REPLY", "req_id": msg.get("req_id"),
                     "service": service, "count": len(servers), "servers": servers}
            sock.sendto(encode(reply), addr)
            self.log.info("QUERY        %s asked for '%s' -> %d result(s)",
                          addr[0], service, len(servers))

    def reaper_loop(self):
        while self.running:
            time.sleep(1.0)
            now = time.time()
            with self.lock:
                dead = [k for k, e in self.table.items() if now - e["last_seen"] > e["ttl"]]
                expired = [self.table.pop(k) for k in dead]
            for e in expired:
                self.log.info("EXPIRED      %s (%s) at %s:%d  (no advert for >%.0fs) -> server DOWN",
                              e["name"], e["service"], e["ip"], e["port"], e["ttl"])
            if expired:
                self.dump()

    def _bind(self, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind_ip, port))
        sock.settimeout(1.0)          # so threads can notice shutdown
        return sock

    def run(self):
        threads = [threading.Thread(target=f, daemon=True)
                   for f in (self.advert_loop, self.query_loop, self.reaper_loop)]
        for t in threads:
            t.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.running = False
            self.log.info("registry stopped")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="UDP service registry")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--ttl", type=float, default=SERVICE_TTL)
    ap.add_argument("--log", default=None, help="log file (default: stdout)")
    a = ap.parse_args()
    Registry(a.bind, a.ttl, a.log).run()
