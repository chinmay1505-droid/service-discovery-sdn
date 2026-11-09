"""BridgedRegistry — Registry subclass that fires event_bus events on every mutation.

Subclasses Registry by overriding:
  register()     → emits REGISTRY/REGISTERED + SDN/ALLOW_FLOW_INSTALLED on first advert
  deregister()   → emits REGISTRY/DEREGISTERED + SDN/ALLOW_FLOW_REMOVED on clean shutdown
  reaper_loop()  → emits REGISTRY/EXPIRED + SDN/ALLOW_FLOW_REMOVED on TTL timeout
  run()          → modified to respect self.running flag for clean shutdown

No networking logic is changed — all UDP socket work remains in the parent class.
"""
import os
import sys
import time

# Resolve path to src/ relative to this file (backend/services/registry_bridge.py)
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from registry import Registry          # noqa: E402  (parent class)
from services import event_bus         # noqa: E402


class BridgedRegistry(Registry):
    """Registry that emits structured events onto event_bus on every table mutation."""

    # ------------------------------------------------------------------ register
    def register(self, ip: str, msg: dict) -> None:
        # Determine whether this is a *new* registration before the parent updates the table.
        try:
            port = int(msg["port"])
            service = str(msg["service"])
            name = str(msg["name"])
            ttl = float(msg.get("ttl", self.default_ttl))
        except (KeyError, TypeError, ValueError):
            super().register(ip, msg)
            return

        key = (ip, port, service)
        with self.lock:
            is_new = key not in self.table

        super().register(ip, msg)  # actual registration logic

        if is_new:
            event_bus.emit("REGISTRY", "REGISTERED", {
                "name": name,
                "service": service,
                "ip": ip,
                "port": port,
                "ttl": ttl,
            })
            # The SDN controller sees this advertisement and installs an ALLOW flow.
            # In standalone mode this event is derived from the registry action.
            event_bus.emit("SDN", "ALLOW_FLOW_INSTALLED", {
                "name": name,
                "service": service,
                "ip": ip,
                "port": port,
                "priority": 300,
                "match": f"udp,ipv4_dst={ip},udp_dst={port}",
                "action": f"output->{name}",
                "reason": f"{name} advertised {service}",
            })
        # Heartbeats are intentionally NOT emitted here to avoid flooding the stream.

    # ------------------------------------------------------------------ deregister
    def deregister(self, ip: str, msg: dict) -> None:
        # Snapshot what will be removed *before* the parent deletes it.
        with self.lock:
            to_remove = [
                (k, e) for k, e in self.table.items()
                if k[0] == ip and (msg.get("service") in (None, k[2]))
            ]

        super().deregister(ip, msg)

        for (key_ip, key_port, key_service), entry in to_remove:
            event_bus.emit("REGISTRY", "DEREGISTERED", {
                "name": entry.get("name", "?"),
                "service": key_service,
                "ip": key_ip,
                "port": key_port,
                "reason": "clean DEREGISTER (graceful shutdown)",
            })
            event_bus.emit("SDN", "ALLOW_FLOW_REMOVED", {
                "name": entry.get("name", "?"),
                "ip": key_ip,
                "port": key_port,
                "reason": "graceful shutdown — DEREGISTER received",
            })

    # ------------------------------------------------------------------ reaper_loop
    def reaper_loop(self) -> None:
        """TTL reaper — identical to parent but emits EXPIRED events."""
        while self.running:
            time.sleep(1.0)
            now = time.time()
            with self.lock:
                dead = [k for k, e in self.table.items()
                        if now - e["last_seen"] > e["ttl"]]
                expired = []
                for k in dead:
                    expired.append((k, self.table.pop(k)))

            for (key_ip, key_port, key_service), entry in expired:
                self.log.info(
                    "EXPIRED      %s (%s) at %s:%d  (no advert for >%.0fs) -> server DOWN",
                    entry["name"], key_service, key_ip, key_port, entry["ttl"],
                )
                event_bus.emit("REGISTRY", "EXPIRED", {
                    "name": entry.get("name", "?"),
                    "service": key_service,
                    "ip": key_ip,
                    "port": key_port,
                    "silent_for": round(now - entry.get("last_seen", now), 1),
                    "ttl": entry.get("ttl", 15),
                })
                event_bus.emit("SDN", "ALLOW_FLOW_REMOVED", {
                    "name": entry.get("name", "?"),
                    "ip": key_ip,
                    "port": key_port,
                    "reason": f"TTL expired after {entry.get('ttl', 15):.0f}s silence",
                })

            if expired:
                self.dump()

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        """Start registry threads; block until self.running is set False."""
        import threading
        threads = [
            threading.Thread(target=f, daemon=True)
            for f in (self.advert_loop, self.query_loop, self.reaper_loop)
        ]
        for t in threads:
            t.start()
        # Parent uses `while True` with KeyboardInterrupt — we use self.running instead.
        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.running = False
        self.log.info("registry stopped")
