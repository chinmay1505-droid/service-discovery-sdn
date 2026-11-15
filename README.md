# Service Discovery SDN — Dynamic Network Control & Observability

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)]()
[![OpenFlow 1.3](https://img.shields.io/badge/OpenFlow-1.3-orange.svg)]()
![Tests: run in the target environment](https://img.shields.io/badge/Tests-run%20locally-lightgrey.svg)

A complete, end-to-end, production-grade university networking system demonstrating **dynamic UDP service discovery integrated with Software-Defined Networking (SDN)**. 

Servers announce themselves autonomously via UDP advertisements, an embedded registry tracks live services, clients perform dynamic discovery, and an **SDN controller (OS-Ken on OpenFlow 1.3 / Open vSwitch)** enforces zero-trust service policies at the switch layer — dynamically installing line-rate `ALLOW` flows for valid servers and revoking them upon heartbeat silence or crash.

The system features a **real-time Web GUI dashboard** connecting to the live socket and SDN layer via WebSockets and REST APIs.

---

## Architecture

```text
                                USER
                                  |
                                  v
                         +----------------+
                         |  Web GUI (NOC) | (HTML5, CSS3, WebSockets)
                         +--------+-------+
                                  |
                            HTTP / WS (:8000)
                                  |
                                  v
                         +----------------+
                         | FastAPI Bridge | (Async Event Bus + REST API)
                         +--------+-------+
                                  |
        +-------------------------+-------------------------+
        |                         |                         |
        v                         v                         v
+---------------+         +---------------+         +---------------+
|    Client     |         |    Registry   |         | Service Server|
|  (src/client) |         | (src/registry)|         |(src/service_s)|
+-------+-------+         +-------+-------+         +-------+-------+
        | (UDP:5001 DISCOVER)     | (UDP:5000 ADVERT)       | (UDP:7000 Echo)
        +-------------------------+-------------------------+
                                  |
                                  v
                         +----------------+
                         |  Mininet / OVS | (sw1, Open vSwitch)
                         +--------+-------+
                                  |
                            OpenFlow 1.3
                                  |
                                  v
                         +----------------+
                         | SDN Controller | (sdn/sdn_controller.py)
                         +----------------+ (OS-Ken: Prio 300/250/200/1/0)
```

### Communication Flow
```mermaid
sequenceDiagram
    participant S as Server (srv1)
    participant SW as Switch (sw1 / OVS)
    participant C as SDN Controller
    participant R as Registry (:5000/:5001)
    participant K as Client (c1)

    Note over SW,C: Switch Connects: Controller installs Table-Miss (0), Default-Deny (200), Snoop (250)
    S->>SW: UDP ADVERTISE (dst: 5000) every 5s
    SW->>C: PacketIn (Priority 250 Snoop Rule)
    C->>C: Learn srv1 IP & port (7000)
    C->>SW: FlowMod (Priority 300 ALLOW udp dst=7000 to srv1)
    C->>SW: PacketOut (forward advert to Registry)
    SW->>R: UDP ADVERTISE
    R->>R: Register srv1, reset TTL (15s)

    K->>R: UDP DISCOVER "chat" (dst: 5001, req_id)
    R->>K: UDP REPLY [srv1@10.0.0.1:7000]

    K->>SW: UDP Service Call to srv1:7000
    Note over SW: Matched Priority 300 ALLOW flow!
    SW->>S: Forward to srv1
    S->>K: UDP Echo Reply "srv1[chat] says: hello"

    Note over S: Server Crashes (SIGKILL) - Silence
    R->>R: Silence > 15s -> EXPIRED event
    C->>SW: Delete Priority 300 ALLOW flow
    Note over SW: Priority 200 DEFAULT-DENY now active
    K->>SW: UDP Service Call to srv1:7000
    Note over SW: DROPPED by Priority 200 Rule
```

---

## OpenFlow 1.3 Priority Hierarchy

| Priority | Tier | OpenFlow Match | Action | Purpose & Mechanism |
|---|---|---|---|---|
| **300** | **ALLOW VALID SERVICE** | `udp, nw_dst=<server>, tp_dst=7000` | `output:port` | Installed dynamically when server advertises. Grants line-rate access to authorized server. |
| **250** | **ADVERTISEMENT SNOOP** | `udp, tp_dst=5000` | `CONTROLLER` | Intercepts all UDP advertisements; controller learns server presence and coordinates flow installation. |
| **200** | **DEFAULT DENY** | `udp, tp_dst=7000` | `drop` | Zero-Trust policy: drops all service traffic unless an explicit priority 300 rule exists. |
| **1** | **L2 FORWARDING** | `in_port, dl_dst` | `output:port` | Learned MAC forwarding for regular network traffic (ARP, ICMP ping). |
| **0** | **TABLE MISS** | `(any)` | `CONTROLLER` | Default fallback: sends unhandled frames as PacketIn to SDN Controller. |

---

## Project Structure

```text
.
├── backend/
│   ├── app.py                      # FastAPI web server, WebSocket /ws, REST API
│   ├── requirements.txt            # Python dependencies (fastapi, uvicorn, websockets)
│   └── services/
│       ├── benchmark_service.py    # Latency benchmarking & static baseline comparison
│       ├── client_runner.py        # Real UDP socket discovery & invocation with event emission
│       ├── event_bus.py            # Thread-safe asyncio event bus for real-time WebSocket push
│       ├── flow_inspector.py       # Live Open vSwitch ovs-ofctl flow parser & SDN tracker
│       ├── process_manager.py      # Subprocess lifecycle manager (start, stop, SIGKILL crash)
│       ├── registry_bridge.py      # BridgedRegistry emitting structured lifecycle events
│       ├── robustness_runner.py    # Robustness scenarios A (Crash), B (Rogue), C (Unknown), D (Loss)
│       ├── test_runner.py          # 17-point automated verification test suite
│       └── topology_service.py     # Mininet / network topology state provider
├── docs/
│   ├── ARCHITECTURE.md             # Comprehensive technical design document
│   └── evidence/                   # Real run execution logs and reports
├── frontend/
│   └── index.html                  # Serious NOC-style Web GUI control panel
├── logs/                           # Runtime logs for registry, controller, and servers
├── sdn/
│   └── sdn_controller.py           # OS-Ken OpenFlow 1.3 controller with dynamic snooping
├── src/
│   ├── client.py                   # Discovery client CLI (with UDP retransmission & latency)
│   ├── common.py                   # Protocol constants, UDP ports, JSON encode/decode
│   ├── registry.py                 # Core registry (UDP 5000 adverts, UDP 5001 queries, TTL reaper)
│   └── service_server.py           # Service server (UDP echo on 7000, periodic adverts)
├── tests/
│   └── run_tests.py                # Standalone CLI test suite runner
├── topo/
│   └── topology.py                 # Mininet topology (sw1, reg, srv1-3, c1-2) & D1 test harness
├── show_flows.sh                   # Shortcut: ovs-ofctl dump-flows sw1
├── start.sh                        # Master launcher for Web GUI and backend
├── start_backend.sh                # Launch FastAPI backend on port 8000
├── start_controller.sh             # Launch OS-Ken SDN controller
└── start_network.sh                # Launch Mininet topology
```

---

## Requirements & Setup

### Environment
* **OS**: Ubuntu Linux 20.04/22.04 LTS (recommended for Mininet + OVS) or macOS (standalone mode)
* **Python**: 3.10+
* **Mininet & Open vSwitch**: (for full SDN topology mode)

### 1. Ubuntu Dependencies Installation
```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch python3-venv ethtool curl
```

### 2. SDN Controller Virtual Environment (One-time)
```bash
python3 -m venv ~/sdn-venv
~/sdn-venv/bin/pip install "os-ken==3.1.1"
```

### 3. Backend & GUI Setup
```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
```

---

## Running the Application

### Mode A: Full Mininet + SDN + Web GUI (Linux/Ubuntu)

Open three terminal windows in the project directory:

#### Terminal 1 — Start SDN Controller
```bash
bash start_controller.sh
```
*Listens on OpenFlow 1.3 port `127.0.0.1:6653`.*

#### Terminal 2 — Start Mininet Network
```bash
bash start_network.sh
```
*Builds topology, connects `sw1` to controller, and starts registry and servers inside hosts.*

#### Terminal 3 — Start Web GUI & Backend
```bash
SDN_MODE=mininet SDN_REGISTRY_IP=10.0.0.100 bash start.sh
```
*Opens web server at `http://localhost:8000` without starting a duplicate host registry. The backend host must be able to reach the Mininet `reg` namespace over UDP; otherwise use the Mininet CLI directly for discovery and service calls.*

---

### Mode B: Standalone Web GUI & Socket System (Any OS / macOS / VM)
If running outside Mininet or evaluating the socket architecture directly:
```bash
bash start.sh
```
Navigate to: **`http://localhost:8000`**

The backend starts the embedded `BridgedRegistry` on UDP `5000` and `5001`. You can launch servers, discover services, run robustness experiments, and benchmark latency directly through the Web GUI or CLI.

#### Explicit backend runtime selection

The default `standalone` mode starts the backend-managed registry and host-managed service processes. To select the separate Mininet registry mode, start the backend with `SDN_MODE=mininet` and optionally set `SDN_REGISTRY_IP` (default `10.0.0.100`). This prevents a second host registry from starting. In Mininet mode the backend queries that registry over UDP and disables host-process server controls and host-based fault injection because they do not execute inside Mininet namespaces. The backend host must have a route to the Mininet registry; if it does not, status and discovery report the registry unavailable. Live flow evidence requires `ovs-ofctl` access to `sw1`.

Example on Linux:
```bash
SDN_MODE=mininet SDN_REGISTRY_IP=10.0.0.100 bash start_backend.sh
```

---

## Live Web GUI Features

The Web GUI is designed as a serious **Network Operations Center (NOC) / SDN Control Layer**:

1. **Dashboard & Discover**:
   - Live system status indicators (Registry, Controller, Server counts, Mode).
   - Real-time search bar with quick-search pills (`chat`, `files`, `*`, `printer`).
   - Visual 5-stage discovery pipeline trace lighting up with real latency metrics.
   - Interactive server cards with one-click "Test UDP Echo" execution.

2. **Services & Registry**:
   - Real-time service registry table with heartbeat age, advert count, and TTL tracking.
   - Server process control panel: launch servers, toggle rogue mode (`--no-advertise`), and inject crashes (`SIGKILL`).

3. **SDN Flow Table**:
   - OpenFlow 1.3 flow table queried directly from Open vSwitch. If OVS is unavailable, the API reports live flow state as unavailable; registry state is not presented as flow evidence.
   - Priority hierarchy explainer explaining why packets are allowed or dropped.

4. **Topology**:
   - Interactive SVG network diagram showing hosts, switch, links, and controller.
   - Real-time link and node status.

5. **Robustness Lab**:
   - Scenario results distinguish pass, fail, and skipped live-environment checks; Mininet namespace fault injection remains a CLI task.

6. **Performance & Baseline**:
   - Repeated UDP discovery and service-call latency measurements with success/failure counts and percentile statistics.
   - Timestamped raw JSON evidence. Static-versus-dynamic comparison is qualitative until equivalent static-endpoint trials are run.

7. **Verification Suite**:
   - 17-point automated verification suite displaying exact test specifications and validation evidence.

8. **Event Stream**:
   - Live WebSocket technical event terminal with source category filtering (`REGISTRY`, `SDN`, `CLIENT`, `SERVER`, `ROBUSTNESS`, `PERF`).

---

## Verification & Automated Testing

### Running the 17-Point Test Suite via CLI
```bash
.venv/bin/python3 tests/run_tests.py
```

### Running Test Suite via Web GUI
Navigate to the **Verification Suite** tab in the GUI and click **`RUN ALL 17 TESTS`**.

### Test Suite Specification

| Domain | Test ID | Description | Validation Detail |
|---|---|---|---|
| **Networking** | `NET-01` | Registry Advert Port Listening | Verifies UDP 5000 actively receives datagrams |
| | `NET-02` | Registry Query Port Listening | Verifies UDP 5001 actively answers queries |
| | `NET-03` | Server UDP Advertisement | Server advertises; registry adds entry |
| | `NET-04` | Wildcard Discovery (`*`) | Returns all active registered services |
| | `NET-05` | Unknown Service Handling | Query for unregistered service returns `count=0` |
| | `NET-06` | End-to-End Service Execution | Client discovers and calls echo service on UDP 7000 |
| **SDN** | `SDN-01` | Table-Miss Rule (Priority 0) | Rule sends unknown frames to controller |
| | `SDN-02` | Advert Snooping (Priority 250) | Rule intercepts UDP 5000 advertisements |
| | `SDN-03` | Default-Deny Policy (Priority 200) | Zero-trust rule drops all UDP 7000 service traffic |
| | `SDN-04` | Dynamic ALLOW Flow (Priority 300) | Installed upon valid advertisement |
| **Robustness** | `ROB-01` | Clean Deregistration | Server sends DEREGISTER on SIGTERM; removed immediately |
| | `ROB-02` | Server Crash & TTL Expiration | Silence exceeding TTL triggers automated purge and flow revocation |
| | `ROB-03` | Unadvertised Server Not Registered | Process startup and registry absence; live packet drop is separate Mininet verification |
| | `ROB-04` | UDP Retransmission Logic | Real client requests against a controlled local UDP loss receiver |
| **API** | `API-01` | Discovery REST Endpoint | Skipped by the CLI runner; requires an ASGI HTTP client |
| | `API-02` | Connect REST Endpoint | Skipped by the CLI runner; requires an ASGI HTTP client |
| | `API-03` | WebSocket Event Delivery | Skipped; event-bus emission alone does not verify WebSocket delivery |

---

## Robustness Scenarios

### Scenario A — Server Crash & TTL Expiry
```text
SERVER ACTIVE  -->  INJECT SIGKILL  -->  HEARTBEAT SILENCE  -->  REGISTRY EXPIRY  -->  SDN FLOW REVOKED
```
- Injects `SIGKILL` to simulate sudden power failure (no `DEREGISTER`).
- The standalone robustness runner uses a short 3-second TTL and verifies registry disappearance.
- Flow removal is reported as skipped unless actual `ovs-ofctl` state is available before and after the crash.

### Scenario B — Rogue Server Defense
```text
ROGUE SERVER (UDP 7000)  -->  NO ADVERTISEMENT  -->  DEFAULT DENY (PRIO 200)  -->  TRAFFIC DROPPED
```
- A rogue host attempts to run a service on UDP 7000 without advertising.
- The current backend runner cannot place a host process inside a Mininet namespace, so it reports packet-drop testing as skipped. Use the Mininet topology test for live switch enforcement.

### Scenario C — Unknown Service Query
- Querying an unregistered service should return `count: 0, servers: []`; latency is measured per run.

### Scenario D — UDP Packet Loss & Retransmission
- A controlled local UDP receiver drops the first two datagrams and replies to the third; the client retry loop is exercised directly.
- Loopback restrictions are reported as skipped rather than as a successful injection.

---

## Performance Evaluation & Baseline Comparison

| Dimension | Static / Baseline Approach | Dynamic SDN System (This Project) | Advantage |
|---|---|---|---|
| **Service Discovery** | Static IP / hardcoded config | Autonomous UDP advertisements & queries | Zero pre-configuration; multi-server agility |
| **Traffic Authorization** | Permissive subnet firewall | Zero-trust default-deny + dynamic Prio 300 ALLOW | Network opens only when server advertises |
| **Failure Handling** | Endpoint updates are manual unless another discovery mechanism is used | Registry TTL expiry and controller flow revocation (live verification required) | Automated lifecycle is implemented in the controller/registry design |
| **Discovery Latency** | No registry query for a configured endpoint | Measured by live benchmark runs | Quantitative comparison requires equivalent trials |
| **Rogue Defense** | Requires maintained host ACLs | Priority 200 default-deny policy (live verification required) | Authorization follows the SDN policy when Mininet is running |
| **Operational Overhead** | O(N * M) manual configuration | O(1) self-registering autonomous nodes | Cloud-native elasticity |

---

## Troubleshooting

- **`Address already in use` on port 5000 or 7000 (macOS)**:
  Apple AirPlay Receiver uses TCP 5000 and 7000. Disable AirPlay Receiver in *System Settings → General → AirDrop & AirPlay → AirPlay Receiver* or kill leftover test processes:
  ```bash
  pkill -f service_server.py
  ```
- **`switch could not reach controller` (Linux)**:
  Start Terminal 1 (`bash start_controller.sh`) before starting Mininet.
- **Mininet cleanup**:
  If a Mininet session crashes, clean old switch state:
  ```bash
  sudo mn -c
  ```

---

## Rubric Mapping

### Deliverable 1 (15 Marks)
* **Problem Understanding & Architecture**: Documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
* **TCP/UDP Socket Implementation**: UDP sockets are implemented in `src/client.py`, `src/registry.py`, `src/service_server.py`, and `backend/services/client_runner.py`.
* **Mininet + SDN**: OpenFlow 1.3 OVS topology (`topo/topology.py`) and OS-Ken controller (`sdn/sdn_controller.py`); live operation requires Linux with Mininet, OVS, and OS-Ken.
* **Socket-SDN Integration**: Controller code maps advertisements to priority 300 allow rules and deregistration/expiry to rule removal; validate with live `ovs-ofctl` output.
* **Initial Testing & Demo**: Run the D1 suite in the documented Linux/Mininet environment and preserve its generated output as evidence.

### Deliverable 2 (25 Marks)
* **Complete Application Functionality**: FastAPI endpoints and dashboard are present; verify in the selected runtime mode.
* **Dynamic SDN Functionality**: Controller and topology implementations are present; live flow lifecycle remains an environment-dependent verification item.
* **Challenging Robustness Scenarios**: Scenarios A–D are exposed. Scenario D injects actual local UDP packet loss and exercises client retries; other SDN-dependent scenarios require a live Mininet environment.
* **Performance Evaluation & Comparison**: Live socket benchmark runner produces timestamped JSON; the static comparison remains qualitative.
* **Documentation, Final Demo & Viva**: Startup scripts and instructions are provided. No 17/17 result is claimed without a run in the target environment.
