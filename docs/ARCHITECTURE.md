# Architecture & Problem Understanding (Deliverable 1)

## 1. Problem in one paragraph
In a network, clients should not need to know in advance *where* a service runs. In this
project, **servers announce ("advertise") themselves by UDP**, a **registry** remembers who is
alive, and **clients ask the registry** which servers offer the service they need. An **SDN
controller** controls the network: only servers that have advertised are allowed to receive
service traffic, and traffic to a server that disappears is blocked again.

## 2. Objectives
| # | Objective (from the project brief) | Where it is implemented |
|---|---|---|
| 1 | Implement UDP advertisements | `src/service_server.py` (advertise thread) |
| 2 | Maintain a service registry | `src/registry.py` (`Registry.table`) |
| 3 | Discover available services | `src/client.py` + registry query thread |
| 4 | Handle service timeouts | registry reaper thread, TTL 15 s |
| 5 | Detect unavailable servers | silence > TTL -> EXPIRED (registry) and flow removed (controller) |
| 6 | Use SDN to control service traffic | `sdn/sdn_controller.py` (allow / default-deny flows) |
| 7 | Support dynamic registration | servers can start/stop any time; no restart needed |
| 8 | Measure discovery latency | `client.py` prints/returns `latency_ms` (D2: full experiments) |

## 3. Components
| Component | Host (Mininet) | Role |
|---|---|---|
| Registry | `reg` 10.0.0.100 | UDP 5000 receives adverts, UDP 5001 answers queries |
| Servers | `srv1` 10.0.0.1 (chat), `srv2` 10.0.0.2 (chat), `srv3` 10.0.0.3 (files) | Offer a UDP echo service on 7000 and advertise every 5 s |
| Clients | `c1` 10.0.0.11, `c2` 10.0.0.12 | Discover, then use a service |
| Switch | `sw1` (Open vSwitch, OpenFlow 1.3) | Forwards packets according to controller rules |
| SDN controller | outside Mininet, 127.0.0.1:6653 | OS-Ken app: learning switch + advert snooping + service policy |

```
   srv1(chat)   srv2(chat)   srv3(files)      c1        c2
        \           |            |            |         /
         \__________|____________|____________|________/
                            [ sw1 ]  <--OpenFlow 1.3-->  [ SDN controller ]
                               |
                          reg (registry)
```

## 4. Communication flow
```mermaid
sequenceDiagram
    participant S as Server (srv1)
    participant SW as Switch sw1
    participant C as SDN Controller
    participant R as Registry
    participant K as Client (c1)
    S->>SW: ADVERTISE (UDP :5000) every 5 s
    SW->>C: PacketIn (snoop rule)
    C->>C: learn srv1; install ALLOW flow (udp to srv1:7000)
    C->>SW: FlowMod + PacketOut (forward advert)
    SW->>R: ADVERTISE
    R->>R: add/refresh entry, reset TTL
    K->>R: DISCOVER "chat" (UDP :5001)
    R->>K: REPLY [srv1, srv2]
    K->>S: service request (UDP :7000)  -- allowed by ALLOW flow
    S->>K: service reply
    Note over S,R: srv1 crashes -> adverts stop
    R->>R: after 15 s: EXPIRED
    C->>SW: after 15 s: delete ALLOW flow -> traffic to srv1 dropped
```

## 5. Application messages (UDP datagrams, JSON text)
| Type | Direction | Fields |
|---|---|---|
| `ADVERTISE` | server -> registry :5000 | `name, service, port, ttl` |
| `DEREGISTER` | server -> registry :5000 | `name, service` (clean shutdown) |
| `DISCOVER` | client -> registry :5001 | `service` (or `*` = all), `req_id` |
| `REPLY` | registry -> client | `req_id, service, count, servers[{name, service, ip, port, age}]` |

Why UDP is fine here: adverts are small and periodic, so a lost one is repaired by the next. For
queries, the client **retransmits** (3 tries, 1 s timeout) and matches the reply by `req_id`.

## 6. SDN design: flow table of `sw1`
| Priority | Match | Action | Installed when |
|---|---|---|---|
| 300 | udp, ipv4_dst = server, udp_dst = 7000 | output to server's port | server advertises |
| 250 | udp, udp_dst = 5000 | send to controller | switch connects |
| 200 | udp, udp_dst = 7000 | **drop** | switch connects |
| 1 | in_port + eth_dst | output (learned) | normal traffic |
| 0 | anything else | send to controller | switch connects |

**Expected network behaviour**
1. Ping/ARP work between all hosts (learning switch).
2. A server that advertises becomes reachable on UDP 7000 (priority 300 beats the drop at 200).
3. A host running a service on UDP 7000 that did **not** advertise is unreachable (drop at 200).
4. When a server's adverts stop for 15 s, its allow flow is deleted, so it is blocked again.

## 7. Socket-SDN integration (the key idea)
The application drives the network: the controller *reads the advertisement datagrams* and turns
them into flow rules. Registering = network opens; going silent = network closes.

## 8. Known limits (honest scope for D1)
* All services use UDP port 7000 so one default-deny rule covers them (can be generalised in D2).
* Single switch topology; multi-switch routing and load balancing are D2 material.
* Latency is measured per query; the multi-condition experiments and static-baseline comparison are D2.
