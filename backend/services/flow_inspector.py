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


def get_flows(registry_table: Dict[Any, Dict[str, Any]] | None = None,
              switch: str = "sw1") -> Dict[str, Any]:
    """Return actual switch flows, or an explicit unavailable result.

    Registry entries are application state and are not evidence that a FlowMod
    reached Open vSwitch.  In particular, never synthesize a flow table from
    ``registry_table`` when the live switch cannot be queried.
    """
    ovs_bin = shutil.which("ovs-ofctl")
    if not ovs_bin:
        return {"available": False, "source": None, "switch": switch,
                "protocol": "OpenFlow 1.3", "count": 0, "flows": [], "raw": None,
                "error": "ovs-ofctl is not installed; live OpenFlow state cannot be verified"}
    try:
        res = subprocess.run(
            [ovs_bin, "-O", "OpenFlow13", "dump-flows", switch],
            capture_output=True, text=True, timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "source": None, "switch": switch,
                "protocol": "OpenFlow 1.3", "count": 0, "flows": [], "raw": None,
                "error": f"Could not query switch {switch}: {exc}"}
    if res.returncode != 0:
        return {"available": False, "source": None, "switch": switch,
                "protocol": "OpenFlow 1.3", "count": 0, "flows": [], "raw": res.stdout,
                "error": (res.stderr.strip() or f"ovs-ofctl exited with status {res.returncode}")}
    parsed = parse_ovs_dump(res.stdout)
    return {"available": True, "source": "ovs-ofctl (Open vSwitch live)",
            "switch": switch, "protocol": "OpenFlow 1.3", "count": len(parsed),
            "flows": parsed, "raw": res.stdout, "error": None}
