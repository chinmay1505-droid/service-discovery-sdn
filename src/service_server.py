#!/usr/bin/env python3
"""A server that offers a service and advertises itself to the registry.

Two jobs, two threads:
  * service thread   : a tiny UDP echo service on UDP 7000 (the thing clients want)
  * advertise thread : sends an ADVERTISE datagram to the registry every N seconds
On Ctrl+C / SIGTERM it sends a DEREGISTER so the registry can drop it immediately.
(If it is killed with SIGKILL, the registry notices by TTL timeout instead.)
"""
import argparse
import signal
import socket
import threading
import time

from common import (REGISTRY_ADVERT_PORT, SERVICE_PORT, ADVERT_INTERVAL,
                    SERVICE_TTL, MAX_DGRAM, encode, get_logger)


def main():
    ap = argparse.ArgumentParser(description="Service server with UDP advertisements")
    ap.add_argument("--name", required=True)
    ap.add_argument("--service", required=True, help="service type, e.g. chat, files")
    ap.add_argument("--registry", required=True, help="registry IP address")
    ap.add_argument("--port", type=int, default=SERVICE_PORT)
    ap.add_argument("--interval", type=float, default=ADVERT_INTERVAL)
    ap.add_argument("--ttl", type=float, default=SERVICE_TTL)
    ap.add_argument("--no-advertise", action="store_true",
                    help="run the service but never register (used to test SDN blocking)")
    ap.add_argument("--log", default=None)
    a = ap.parse_args()
    log = get_logger(a.name, a.log)
    stop = threading.Event()

    # ---- service thread: UDP echo ----
    svc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    svc.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    svc.bind(("0.0.0.0", a.port))
    svc.settimeout(1.0)

    def serve():
        while not stop.is_set():
            try:
                data, addr = svc.recvfrom(MAX_DGRAM)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "replace")
            svc.sendto(("%s[%s] says: %s" % (a.name, a.service, text)).encode(), addr)
            log.info("served request from %s: %r", addr[0], text)

    # ---- advertise thread ----
    adv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (a.registry, REGISTRY_ADVERT_PORT)
    advert = {"type": "ADVERTISE", "name": a.name, "service": a.service,
              "port": a.port, "ttl": a.ttl}

    def advertise():
        n = 0
        while not stop.is_set():
            try:
                adv.sendto(encode(advert), target)
                n += 1
                if n == 1 or n % 6 == 0:
                    log.info("advertised %s to %s:%d (#%d)", a.service, *target, n)
            except OSError as exc:
                log.info("advert send failed: %s", exc)
            stop.wait(a.interval)

    def shutdown(*_):
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threading.Thread(target=serve, daemon=True).start()
    if a.no_advertise:
        log.info("%s running WITHOUT advertising (service on UDP %d)", a.name, a.port)
    else:
        threading.Thread(target=advertise, daemon=True).start()
        log.info("%s offering '%s' on UDP %d, advertising to %s:%d every %.0fs",
                 a.name, a.service, a.port, *target, a.interval)

    while not stop.is_set():
        time.sleep(0.2)
    if not a.no_advertise:
        try:
            adv.sendto(encode({"type": "DEREGISTER", "name": a.name,
                               "service": a.service}), target)
            log.info("sent DEREGISTER, bye")
        except OSError:
            pass


if __name__ == "__main__":
    main()
