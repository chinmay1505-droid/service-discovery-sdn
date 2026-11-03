#!/usr/bin/env python3
"""Mininet topology for the Service Discovery System.

                         +-----------+
   srv1 (chat)  ---------|           |--------- c1 (client)
   srv2 (chat)  ---------|    sw1    |--------- c2 (client / rogue test host)
   srv3 (files) ---------|  (OVS, OF1.3)
   reg  (registry) ------|           |========= SDN controller (127.0.0.1:6653)
                         +-----------+

Usage (start the controller FIRST, in another terminal):
  sudo python3 topo/topology.py            # build network, start apps, open Mininet CLI
  sudo python3 topo/topology.py --test     # build network, run the D1 test cases, print report
  sudo python3 topo/topology.py --no-apps  # network only; start apps by hand
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time

from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SRC, LOGS = os.path.join(ROOT, "src"), os.path.join(ROOT, "logs")
PY = sys.executable or "python3"

REG_IP = "10.0.0.100"
SERVERS = {"srv1": ("10.0.0.1", "chat"), "srv2": ("10.0.0.2", "chat"), "srv3": ("10.0.0.3", "files")}
CLIENTS = {"c1": "10.0.0.11", "c2": "10.0.0.12"}


class DiscoveryTopo(Topo):
    def build(self):
        sw = self.addSwitch("sw1", protocols="OpenFlow13")
        self.addLink(self.addHost("reg", ip=REG_IP + "/24"), sw)
        for name, (ip, _svc) in SERVERS.items():
            self.addLink(self.addHost(name, ip=ip + "/24"), sw)
        for name, ip in CLIENTS.items():
            self.addLink(self.addHost(name, ip=ip + "/24"), sw)


def make_switch(datapath):
    def factory(name, **kw):
        kw.setdefault("protocols", "OpenFlow13")
        return OVSSwitch(name, datapath=datapath, **kw)
    return factory


class Apps:
    """Starts/stops the registry and servers inside the Mininet hosts."""
    def __init__(self, net):
        self.net, self.procs = net, {}
        os.makedirs(LOGS, exist_ok=True)

    def start_registry(self):
        self._run("reg", ["registry.py", "--log", os.path.join(LOGS, "registry.log")])

    def start_server(self, host, name, service, extra=()):
        self._run(host, ["service_server.py", "--name", name, "--service", service,
                         "--registry", REG_IP, "--log", os.path.join(LOGS, name + ".log"), *extra])

    def _run(self, host, args):
        cmd = [PY, "-u", os.path.join(SRC, args[0])] + args[1:]
        key = host + ":" + args[0] + ":" + (args[args.index("--name") + 1] if "--name" in args else "")
        self.procs[key] = self.net.get(host).popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    def kill(self, host, hard=False):
        for key, p in list(self.procs.items()):
            if key.startswith(host + ":service_server"):
                p.kill() if hard else p.terminate()
                p.wait()
                del self.procs[key]

    def stop_all(self):
        for p in self.procs.values():
            try:
                p.terminate()
            except Exception:
                pass
        self.procs.clear()

    def start_all(self):
        self.start_registry()
        time.sleep(1)
        for name, (_ip, svc) in SERVERS.items():
            self.start_server(name, name, svc)


# ------------------------------------------------------------------ test helpers
def client(net, host, *args):
    out = net.get(host).cmd(" ".join(shlex.quote(x) for x in (PY, os.path.join(SRC, "client.py"), *args, "--json")))
    for line in reversed(out.strip().splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return {"ok": False, "raw": out}


def flows(net):
    return subprocess.run(["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "sw1"],
                          capture_output=True, text=True).stdout


def run_tests(net, apps):
    results = []

    def record(tid, title, ok, detail):
        results.append((tid, title, ok, detail))
        print("  [%s] %s  %s -- %s" % ("PASS" if ok else "FAIL", tid, title, detail), flush=True)

    print("\n=== D1 test cases ===")
    # T1 -- basic connectivity through the SDN-controlled switch
    loss = net.pingAll(timeout=2)
    record("T1", "Host connectivity via SDN switch (pingAll)", loss == 0, "packet loss %.0f%%" % loss)

    apps.start_all()
    print("  ... waiting 8s for advertisements ...")
    time.sleep(8)

    # T2 -- registry learned every advertised server
    r = client(net, "c1", "--registry", REG_IP, "--service", "*")
    names = sorted(s["name"] for s in r.get("servers", []))
    record("T2", "Registry holds all advertised servers", names == ["srv1", "srv2", "srv3"], "found %s" % names)

    # T3 -- discovery filters by service type
    r = client(net, "c1", "--registry", REG_IP, "--service", "chat")
    names = sorted(s["name"] for s in r.get("servers", []))
    record("T3", "Discover 'chat' -> srv1 + srv2", names == ["srv1", "srv2"],
           "found %s, latency %.2f ms" % (names, r.get("latency_ms", -1)))
    r = client(net, "c1", "--registry", REG_IP, "--service", "files")
    names = [s["name"] for s in r.get("servers", [])]
    record("T4", "Discover 'files' -> srv3", names == ["srv3"], "found %s, latency %.2f ms" % (names, r.get("latency_ms", -1)))

    # T5 -- unknown service
    r = client(net, "c1", "--registry", REG_IP, "--service", "printer")
    record("T5", "Discover unknown service -> empty list", r.get("ok") and r.get("count") == 0, "count=%s" % r.get("count"))

    # T6 -- end to end: discover, then actually use the service through the SDN allow-flow
    r = client(net, "c2", "--registry", REG_IP, "--service", "files", "--use", "--message", "hello-from-c2")
    record("T6", "End-to-end: discover then use service (c2 -> srv3)", bool(r.get("ok") and r.get("reply")),
           "reply=%r" % r.get("reply"))

    # T7 -- SDN flow rules were created by the controller as a result of advertisements
    fl = flows(net)
    allow_ok = all(("nw_dst=%s" % ip) in fl and "tp_dst=7000" in fl for ip, _ in SERVERS.values())
    record("T7", "Controller installed ALLOW flows for advertised servers", allow_ok,
           "%d allow-flows seen" % sum(("nw_dst=%s" % ip) in fl for ip, _ in SERVERS.values()))

    # T8 -- unregistered (rogue) service is blocked by the SDN default-deny rule
    apps.start_server("c2", "rogue", "chat", extra=["--no-advertise"])
    time.sleep(1)
    ping_ok = net.get("c1").cmd("ping -c 2 -W 1 %s" % CLIENTS["c2"]).count(" 0% packet loss") == 1
    r = client(net, "c1", "--direct", CLIENTS["c2"] + ":7000", "--message", "hi")
    record("T8", "Un-advertised server is BLOCKED by SDN (ping ok, udp/7000 dropped)",
           ping_ok and not r.get("ok"), "ping %s, udp reply=%r" % ("ok" if ping_ok else "FAILED", r.get("reply")))
    for k in [k for k in apps.procs if k.startswith("c2:")]:
        apps.procs.pop(k).terminate()

    # T9 -- bonus (D2 preview): crashed server expires and gets blocked
    apps.kill("srv1", hard=True)
    print("  ... srv1 killed (no goodbye); waiting 20s for timeout ...")
    time.sleep(20)
    r = client(net, "c1", "--registry", REG_IP, "--service", "chat")
    names = sorted(s["name"] for s in r.get("servers", []))
    fl = flows(net)
    flow_gone = "nw_dst=10.0.0.1" not in fl
    blocked = not client(net, "c1", "--direct", "10.0.0.1:7000", "--message", "x").get("ok")
    record("T9", "Crashed server expires from registry AND traffic to it is blocked",
           names == ["srv2"] and flow_gone and blocked,
           "chat now %s, allow-flow removed=%s, traffic blocked=%s" % (names, flow_gone, blocked))

    passed = sum(1 for r in results if r[2])
    print("\n=== %d / %d tests passed ===" % (passed, len(results)))
    with open(os.path.join(LOGS, "d1_test_report.txt"), "w") as f:
        f.write("D1 test report  (%s)\n\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        for tid, title, ok, detail in results:
            f.write("%-3s %-5s %s -- %s\n" % (tid, "PASS" if ok else "FAIL", title, detail))
        f.write("\n%d / %d passed\n\nFlow table at end of run:\n%s" % (passed, len(results), flows(net)))
    return passed == len(results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="run automated D1 test cases")
    ap.add_argument("--no-apps", action="store_true", help="do not auto-start registry/servers")
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--controller-port", type=int, default=6653)
    ap.add_argument("--datapath", default="kernel", choices=["kernel", "user"],
                    help="OVS datapath; use 'user' only if the kernel module is unavailable")
    a = ap.parse_args()
    setLogLevel("info")
    net = Mininet(topo=DiscoveryTopo(), switch=make_switch(a.datapath), controller=None, waitConnected=False)
    net.addController("c0", controller=RemoteController, ip=a.controller_ip, port=a.controller_port)
    net.start()
    if not net.waitConnected(timeout=10):
        print("\nERROR: switch could not reach the SDN controller at %s:%d.\n"
              "Start it first:  osken-manager sdn/sdn_controller.py\n" % (a.controller_ip, a.controller_port))
        net.stop()
        sys.exit(1)
    if a.datapath == "user":      # user-space switch cannot fix partial checksums -> turn offload off
        for h in net.hosts:
            h.cmd("ethtool -K %s-eth0 tx off rx off >/dev/null 2>&1" % h.name)
    apps = Apps(net)
    ok = True
    try:
        if a.test:
            ok = run_tests(net, apps)
        else:
            if not a.no_apps:
                apps.start_all()
                print("\nRegistry + 3 servers started (logs in %s)." % LOGS)
            print("Try:  c1 python3 src/client.py --registry %s --service chat --use\n" % REG_IP)
            CLI(net)
    finally:
        apps.stop_all()
        net.stop()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
