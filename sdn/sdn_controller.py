"""SDN controller (OS-Ken, the maintained fork of Ryu; same API).

What it does
------------
1. Learning switch  : normal connectivity (ARP, ping, replies) via MAC learning.
2. Advert snooping  : every UDP advertisement (dst port 5000) is sent to the controller,
                      which reads the JSON and learns "server X at IP:port is alive".
3. Service policy   : by default ALL UDP traffic to the service port (7000) is DROPPED.
                      When a server advertises, the controller installs a higher-priority
                      flow that ALLOWS traffic to exactly that server.
4. Failure handling : if a server stops advertising, the controller deletes its allow-flow,
                      so traffic to it is dropped again.

So the application (advertisements) directly drives the network (flow rules)  ->  Socket-SDN integration.

Flow table priorities
---------------------
 300  udp, ipv4_dst=<server>, udp_dst=<svc port>   -> output(server port)   (installed on advert)
 250  udp, udp_dst=5000                            -> controller           (advert snooping)
 200  udp, udp_dst=7000                            -> drop                 (default-deny for services)
   1  in_port, eth_dst                             -> output(port)         (learned L2 forwarding)
   0  table-miss                                   -> controller
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from common import REGISTRY_ADVERT_PORT, SERVICE_PORT, SERVICE_TTL  # noqa: E402

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.lib import hub
from os_ken.lib.packet import ethernet, ether_types, ipv4, packet, udp
from os_ken.ofproto import ofproto_v1_3

PRIO_ALLOW, PRIO_SNOOP, PRIO_DENY, PRIO_L2, PRIO_MISS = 300, 250, 200, 1, 0


class ServiceDiscoverySDN(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}      # dpid -> {mac: port}
        self.services = {}         # (ip, port) -> {name, service, dp, out_port, last_seen}
        hub.spawn(self._monitor)

    # ------------------------------------------------------------ helpers
    def _add_flow(self, dp, priority, match, actions, idle=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=inst, idle_timeout=idle))

    def _del_flow(self, dp, priority, match):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE_STRICT,
                                      priority=priority, match=match,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY))

    @staticmethod
    def _allow_match(parser, ip, port):
        return parser.OFPMatch(eth_type=0x0800, ip_proto=17, ipv4_dst=ip, udp_dst=port)

    # ------------------------------------------------------------ switch connects
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        to_ctrl = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, PRIO_MISS, parser.OFPMatch(), to_ctrl)
        self._add_flow(dp, PRIO_SNOOP, parser.OFPMatch(
            eth_type=0x0800, ip_proto=17, udp_dst=REGISTRY_ADVERT_PORT), to_ctrl)
        self._add_flow(dp, PRIO_DENY, parser.OFPMatch(
            eth_type=0x0800, ip_proto=17, udp_dst=SERVICE_PORT), [])      # [] = drop
        self.logger.info("[SDN] switch %016x connected: installed table-miss, "
                         "advert-snoop (udp/%d) and default-deny (udp/%d) rules",
                         dp.id, REGISTRY_ADVERT_PORT, SERVICE_PORT)

    # ------------------------------------------------------------ packet-in
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        table = self.mac_to_port.setdefault(dp.id, {})
        table[eth.src] = in_port                                  # learn

        ip4, u = pkt.get_protocol(ipv4.ipv4), pkt.get_protocol(udp.udp)
        if ip4 and u and u.dst_port == REGISTRY_ADVERT_PORT:
            payload = pkt.protocols[-1]
            if isinstance(payload, (bytes, bytearray)):
                self._on_advert(dp, in_port, ip4.src, bytes(payload))

        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]
        is_snooped = bool(ip4 and u and u.dst_port == REGISTRY_ADVERT_PORT)
        if out_port != ofp.OFPP_FLOOD and not is_snooped:         # snooped adverts must keep coming to us
            self._add_flow(dp, PRIO_L2, parser.OFPMatch(in_port=in_port, eth_dst=eth.dst), actions)
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=in_port, actions=actions, data=msg.data))

    # ------------------------------------------------------------ application events -> flow rules
    def _on_advert(self, dp, in_port, src_ip, payload):
        try:
            m = json.loads(payload.decode("utf-8"))
            mtype = m["type"]
        except (ValueError, KeyError, UnicodeDecodeError):
            return
        parser = dp.ofproto_parser
        if mtype == "ADVERTISE":
            try:
                name, service, port = str(m["name"]), str(m["service"]), int(m["port"])
            except (KeyError, TypeError, ValueError):
                return
            key = (src_ip, port)
            known = key in self.services
            self.services[key] = {"name": name, "service": service, "dp": dp,
                                  "out_port": in_port, "last_seen": time.time()}
            if not known:
                self._add_flow(dp, PRIO_ALLOW, self._allow_match(parser, src_ip, port),
                               [parser.OFPActionOutput(in_port)])
                self.logger.info("[SDN] ADVERT from %s (%s@%s:%d) -> installed ALLOW flow "
                                 "(udp to %s:%d -> switch port %d)",
                                 name, service, src_ip, port, src_ip, port, in_port)
        elif mtype == "DEREGISTER":
            for key in [k for k in self.services if k[0] == src_ip]:
                self._remove(key, "clean DEREGISTER")

    def _remove(self, key, reason):
        svc = self.services.pop(key, None)
        if svc:
            dp = svc["dp"]
            self._del_flow(dp, PRIO_ALLOW, self._allow_match(dp.ofproto_parser, key[0], key[1]))
            self.logger.info("[SDN] %s (%s:%d) %s -> removed ALLOW flow, traffic now BLOCKED",
                             svc["name"], key[0], key[1], reason)

    def _monitor(self):
        """Every second: servers that stopped advertising lose their allow-flow."""
        while True:
            hub.sleep(1.0)
            now = time.time()
            for key, svc in list(self.services.items()):
                if now - svc["last_seen"] > SERVICE_TTL:
                    self._remove(key, "silent for >%.0fs" % SERVICE_TTL)
