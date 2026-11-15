"""Topology service.

Reflects the actual Mininet / network topology with live status
for switch, controller, registry, servers, and clients.
"""
from typing import Dict, Any, List

# Topology definition matching topo/topology.py
DEFAULT_SERVERS = {
    "srv1": {"ip": "10.0.0.1", "default_service": "chat", "port": 7000},
    "srv2": {"ip": "10.0.0.2", "default_service": "chat", "port": 7000},
    "srv3": {"ip": "10.0.0.3", "default_service": "files", "port": 7000},
}

DEFAULT_CLIENTS = {
    "c1": {"ip": "10.0.0.11", "role": "Discovery Client"},
    "c2": {"ip": "10.0.0.12", "role": "Client / Rogue Test Host"},
}

REGISTRY_NODE = {
    "id": "reg",
    "name": "reg",
    "type": "registry",
    "label": "Service Registry",
    "ip": "10.0.0.100",
    "ports": "UDP 5000 / 5001",
}

SWITCH_NODE = {
    "id": "sw1",
    "name": "sw1",
    "type": "switch",
    "label": "Open vSwitch (sw1)",
    "protocol": "OpenFlow 1.3",
    "dpid": "0000000000000001",
}

CONTROLLER_NODE = {
    "id": "c0",
    "name": "c0",
    "type": "controller",
    "label": "OS-Ken SDN Controller",
    "address": "127.0.0.1:6653",
    "protocol": "OpenFlow 1.3",
}


def get_topology(registry_table: Dict[Any, Dict[str, Any]], controller_status: Dict[str, Any],
                 mode: str = "standalone", flows_available: bool = False) -> Dict[str, Any]:
    """Build a topology view while separating registry facts from flow evidence."""
    nodes = []
    links = []

    # 1. Switch
    nodes.append({
        **SWITCH_NODE,
        "status": "UNVERIFIED",
    })

    # 2. Controller
    nodes.append({
        **CONTROLLER_NODE,
        "status": "TCP_LISTENER_REACHABLE" if controller_status.get("tcp_listener_reachable") else "UNREACHABLE",
    })
    links.append({
        "source": "c0",
        "target": "sw1",
        "type": "control",
        "protocol": "OpenFlow 1.3",
        "status": "UNVERIFIED",
    })

    # 3. Registry
    reg_online = bool(controller_status.get("registry_online", mode == "standalone"))
    nodes.append({
        **REGISTRY_NODE,
        "status": "ONLINE" if reg_online else "OFFLINE",
        "entries": len(registry_table),
    })
    links.append({
        "source": "reg",
        "target": "sw1",
        "type": "data",
        "status": "ACTIVE",
    })

    # 4. Servers (both default hosts and any registered servers)
    known_servers = dict(DEFAULT_SERVERS)
    # Check which servers are actively registered
    active_names = {}
    for (ip, port, svc), entry in registry_table.items():
        name = entry.get("name", "srv")
        active_names[name] = entry
        if name not in known_servers:
            known_servers[name] = {"ip": ip, "default_service": svc, "port": port}

    for name, sinfo in known_servers.items():
        is_active = name in active_names
        entry = active_names.get(name, {})
        nodes.append({
            "id": name,
            "name": name,
            "type": "server",
            "label": f"Server {name}",
            "ip": entry.get("ip", sinfo["ip"]),
            "port": entry.get("port", sinfo["port"]),
            "service": entry.get("service", sinfo["default_service"]),
            "status": "ACTIVE" if is_active else "OFFLINE",
            "adverts": entry.get("adverts", 0),
            "flow_status": ("LIVE_FLOW_TABLE_AVAILABLE" if flows_available and is_active
                            else ("UNVERIFIED" if is_active else "NO_REGISTRY_ENTRY")),
        })
        links.append({
            "source": name,
            "target": "sw1",
            "type": "data",
            "status": "ACTIVE" if is_active else "IDLE",
        })

    # 5. Clients
    for name, cinfo in DEFAULT_CLIENTS.items():
        nodes.append({
            "id": name,
            "name": name,
            "type": "client",
            "label": f"Client {name}",
            "ip": cinfo["ip"],
            "role": cinfo["role"],
            "status": "ONLINE",
        })
        links.append({
            "source": name,
            "target": "sw1",
            "type": "data",
            "status": "ACTIVE",
        })

    return {
        "mode": mode,
        "flow_state_verified": flows_available,
        "switch": "sw1",
        "controller": "c0",
        "nodes": nodes,
        "links": links,
        "summary": {
            "total_nodes": len(nodes),
            "total_links": len(links),
            "active_servers": len(active_names),
            "controller_tcp_listener_reachable": controller_status.get("tcp_listener_reachable", False),
            "controller_online": controller_status.get("tcp_listener_reachable", False),
        },
    }
