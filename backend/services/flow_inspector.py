"""SDN Flow Inspector.

Reads flows directly from Open vSwitch via ovs-ofctl when available,
or inspects active SDN policies tracked by the controller / registry bridge.
"""
import re
import shutil
import subprocess
import time
from typing import List, Dict, Any

PRIORITY_EXPLANATIONS = {
    300: {
        "tier": "ALLOW VALID SERVICE",
        "purpose": "Authorized service traffic allowed to specific registered server IP/port",
        "description": "Installed dynamically by SDN controller when a server advertises. Beats the drop rule at 200.",
        "badge": "ALLOW",
        "type": "dynamic",
    },
    250: {
        "tier": "ADVERTISEMENT SNOOPING",
        "purpose": "UDP port 5000 datagrams forwarded to controller",
        "description": "Intercepts service announcements and deregistration messages so SDN learns server presence.",
        "badge": "SNOOP",
        "type": "static",
    },
    200: {
        "tier": "DEFAULT DENY",
        "purpose": "All UDP port 7000 traffic DROPPED",
        "description": "Blocks any service traffic unless specifically authorized by a priority 300 rule (Zero Trust).",
        "badge": "DROP",
        "type": "security",
    },
    1: {
        "tier": "L2 FORWARDING",
        "purpose": "Normal Ethernet unicast forwarding",
        "description": "Dynamic MAC learning table for normal network traffic (ARP, ICMP ping, etc.).",
        "badge": "FORWARD",
        "type": "learning",
    },
    0: {
        "tier": "TABLE MISS",
        "purpose": "Send unknown packets to SDN Controller",
        "description": "Base OpenFlow 1.3 rule ensuring unhandled frames trigger PacketIn to the controller.",
        "badge": "CONTROLLER",
        "type": "base",
    },
}


def parse_ovs_dump(raw_text: str) -> List[Dict[str, Any]]:
    """Parse output from ovs-ofctl dump-flows into structured dicts."""
    flows = []
    lines = raw_text.strip().splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("NXST") or line.startswith("OFPST"):
            continue

        # Extract tokens
        # Example: cookie=0x0, duration=15.123s, table=0, n_packets=4, n_bytes=320, priority=300,udp,nw_dst=10.0.0.1,tp_dst=7000 actions=output:"sw1-eth1"
        actions = ""
        match_part = line
        if "actions=" in line:
            parts = line.split("actions=", 1)
            match_part = parts[0].strip().rstrip(",")
            actions = parts[1].strip()

        # Extract stats
        cookie = ""
        duration = ""
        table = "0"
        n_packets = "0"
        n_bytes = "0"
        priority = 0

        m_cookie = re.search(r"cookie=(0x[0-9a-fA-F]+|[0-9]+)", match_part)
        if m_cookie:
            cookie = m_cookie.group(1)

        m_dur = re.search(r"duration=([0-9.]+s?)", match_part)
        if m_dur:
            duration = m_dur.group(1)

        m_tbl = re.search(r"table=([0-9]+)", match_part)
        if m_tbl:
            table = m_tbl.group(1)

        m_pkts = re.search(r"n_packets=([0-9]+)", match_part)
        if m_pkts:
            n_packets = m_pkts.group(1)

        m_bytes = re.search(r"n_bytes=([0-9]+)", match_part)
        if m_bytes:
            n_bytes = m_bytes.group(1)

        m_prio = re.search(r"priority=([0-9]+)", match_part)
        if m_prio:
            priority = int(m_prio.group(1))

        # Clean match string: remove cookie, duration, table, n_packets, n_bytes, priority
        clean_match = match_part
        for pattern in [r"cookie=[^,]+,?", r"duration=[^,]+,?", r"table=[^,]+,?",
                        r"n_packets=[^,]+,?", r"n_bytes=[^,]+,?", r"priority=[0-9]+,?"]:
            clean_match = re.sub(pattern, "", clean_match)
        clean_match = clean_match.strip().strip(",")

        expl = PRIORITY_EXPLANATIONS.get(priority, {
            "tier": f"Priority {priority}",
            "purpose": "Custom OpenFlow Rule",
            "description": "Flow rule installed on switch",
            "badge": "RULE",
            "type": "custom",
        })

        flows.append({
            "cookie": cookie,
            "duration": duration,
            "table": table,
            "n_packets": int(n_packets) if n_packets.isdigit() else 0,
            "n_bytes": int(n_bytes) if n_bytes.isdigit() else 0,
            "priority": priority,
            "match": clean_match or "(any)",
            "actions": actions or "drop",
            "tier": expl["tier"],
            "purpose": expl["purpose"],
            "description": expl["description"],
            "badge": expl["badge"],
            "type": expl["type"],
            "source": "ovs-ofctl",
        })

    flows.sort(key=lambda f: f["priority"], reverse=True)
    return flows


def get_flows(registry_table: Dict[Any, Dict[str, Any]]) -> Dict[str, Any]:
    """Retrieve flows either from OVS via ovs-ofctl or from active controller state."""
    # Check if ovs-ofctl is available
    ovs_bin = shutil.which("ovs-ofctl")
    if ovs_bin:
        try:
            res = subprocess.run(
                ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "sw1"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                parsed = parse_ovs_dump(res.stdout)
                return {
                    "source": "ovs-ofctl (Open vSwitch live)",
                    "switch": "sw1",
                    "protocol": "OpenFlow 1.3",
                    "count": len(parsed),
                    "flows": parsed,
                    "raw": res.stdout,
                }
        except Exception:
            pass

    # Standalone or controller-tracked flow generation
    # Synthesizes the exact OpenFlow 1.3 flow table maintained by sdn_controller.py
    flows = []

    # Priority 300: Dynamic ALLOW flows for each registered server
    for (ip, port, service), entry in registry_table.items():
        name = entry.get("name", "server")
        flows.append({
            "cookie": "0x0",
            "duration": f"{round(time.time() - entry.get('first_seen', time.time()), 1)}s",
            "table": "0",
            "n_packets": entry.get("adverts", 1),
            "n_bytes": entry.get("adverts", 1) * 64,
            "priority": 300,
            "match": f"udp,ipv4_dst={ip},udp_dst={port}",
            "actions": f"output:{name}",
            "server": name,
            "service": service,
            **PRIORITY_EXPLANATIONS[300],
            "source": "controller_state",
        })

    # Priority 250: Advert snooping
    flows.append({
        "cookie": "0x0",
        "duration": "active",
        "table": "0",
        "n_packets": sum(e.get("adverts", 0) for e in registry_table.values()),
        "n_bytes": sum(e.get("adverts", 0) for e in registry_table.values()) * 128,
        "priority": 250,
        "match": "udp,udp_dst=5000",
        "actions": "CONTROLLER:65535",
        **PRIORITY_EXPLANATIONS[250],
        "source": "controller_state",
    })

    # Priority 200: Default deny for service port 7000
    flows.append({
        "cookie": "0x0",
        "duration": "active",
        "table": "0",
        "n_packets": 0,
        "n_bytes": 0,
        "priority": 200,
        "match": "udp,udp_dst=7000",
        "actions": "drop",
        **PRIORITY_EXPLANATIONS[200],
        "source": "controller_state",
    })

    # Priority 1: L2 MAC learning
    flows.append({
        "cookie": "0x0",
        "duration": "active",
        "table": "0",
        "n_packets": 0,
        "n_bytes": 0,
        "priority": 1,
        "match": "in_port=*,eth_dst=*",
        "actions": "output:port (learned)",
        **PRIORITY_EXPLANATIONS[1],
        "source": "controller_state",
    })

    # Priority 0: Table-miss
    flows.append({
        "cookie": "0x0",
        "duration": "active",
        "table": "0",
        "n_packets": 0,
        "n_bytes": 0,
        "priority": 0,
        "match": "(any)",
        "actions": "CONTROLLER:65535",
        **PRIORITY_EXPLANATIONS[0],
        "source": "controller_state",
    })

    flows.sort(key=lambda f: f["priority"], reverse=True)
    return {
        "source": "controller-tracked (OpenFlow 1.3 policy)",
        "switch": "sw1",
        "protocol": "OpenFlow 1.3",
        "count": len(flows),
        "flows": flows,
        "raw": None,
    }
