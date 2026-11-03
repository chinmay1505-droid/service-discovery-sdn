"""Shared constants and helpers for the UDP Service Discovery System."""
import json
import logging
import sys

# ---- Network constants (the SDN controller imports these too) ----
REGISTRY_ADVERT_PORT = 5000   # servers  -> registry  (ADVERTISE / DEREGISTER)
REGISTRY_QUERY_PORT = 5001    # clients  -> registry  (DISCOVER / REPLY)
SERVICE_PORT = 7000           # the actual service that servers offer (UDP echo)
ADVERT_INTERVAL = 5.0         # seconds between advertisements
SERVICE_TTL = 15.0            # seconds of silence before a server is "down"
MAX_DGRAM = 2048              # max UDP datagram we accept


def encode(msg: dict) -> bytes:
    """dict -> JSON bytes (application-level message format)."""
    return json.dumps(msg, separators=(",", ":")).encode("utf-8")


def decode(data: bytes):
    """JSON bytes -> dict, or None if the datagram is malformed."""
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(msg, dict) or "type" not in msg:
        return None
    return msg


def get_logger(name: str, logfile: str = None) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(message)s", "%H:%M:%S")
    handler = logging.FileHandler(logfile, mode="w") if logfile else logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    log.addHandler(handler)
    return log
