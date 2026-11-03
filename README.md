# SDN-Socket Jackfruit Mini Project #21 — UDP Service Discovery System

Servers **advertise** themselves over UDP, a **registry** keeps the list of live services, clients
**discover** services, and an **SDN controller** (OS-Ken + Open vSwitch in Mininet) only allows
traffic to servers that have advertised.

* Architecture and design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
* Test evidence from a real run: [`docs/evidence/`](docs/evidence/)

## Repository layout
```
src/common.py          shared constants + JSON message helpers
src/registry.py        registry (UDP 5000 adverts, UDP 5001 queries, TTL reaper)
src/service_server.py  server: UDP echo service on 7000 + periodic advertisements
src/client.py          client: discover (with retries + latency), optionally use the service
sdn/sdn_controller.py  OS-Ken controller: learning switch, advert snooping, allow/deny flows
topo/topology.py       Mininet topology, app launcher, automated test cases
docs/                  architecture + evidence
logs/                  runtime logs (created when you run)
```

## One-time setup (Ubuntu VM)
Mininet and Open vSwitch are already installed (`sudo apt install mininet`). Install the controller
framework in a virtual environment (the pinned version matters: newer OS-Ken releases removed the
`osken-manager` launcher):
```bash
sudo apt install -y python3-venv ethtool
python3 -m venv ~/sdn-venv
~/sdn-venv/bin/pip install "os-ken==3.1.1"
```

## Run it (two terminals, both in the project folder)
**Terminal 1 – controller**
```bash
mkdir -p logs
~/sdn-venv/bin/osken-manager sdn/sdn_controller.py 2>&1 | tee logs/controller.log
```
Leave it running. You should see `loading app sdn/sdn_controller.py`.

**Terminal 2 – network + apps**
```bash
sudo mn -c                       # clean old state (always safe)
sudo python3 topo/topology.py    # builds the network, starts registry + 3 servers, opens mininet>
```
At the `mininet>` prompt:
```
mininet> c1 python3 src/client.py --registry 10.0.0.100 --service chat
mininet> c1 python3 src/client.py --registry 10.0.0.100 --service chat --use
mininet> c1 python3 src/client.py --registry 10.0.0.100 --service '*'
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows sw1          # see the controller's flow rules
mininet> sh pkill -9 -f '[-]-name srv1'                     # crash srv1 (no goodbye); watch it expire after 15 s
mininet> c1 python3 src/client.py --registry 10.0.0.100 --service chat   # srv1 is gone
```
Watch the live logs in a third terminal: `tail -f logs/registry.log` (and `logs/srv1.log`, ...).

## Automated D1 test cases
```bash
sudo mn -c
sudo python3 topo/topology.py --test
```
Prints PASS/FAIL for 9 test cases and writes `logs/d1_test_report.txt` (takes ~45 s).

| ID | Test | Shows |
|---|---|---|
| T1 | `pingAll` | switch + controller connectivity |
| T2 | Registry lists all 3 servers | UDP advertisements work |
| T3/T4 | Discover `chat` -> srv1+srv2, `files` -> srv3 | filtering by service |
| T5 | Unknown service -> empty list | correct negative answer |
| T6 | Discover then use service | end-to-end through the SDN switch |
| T7 | Allow flows exist in the switch | advert -> flow rule (Socket-SDN integration) |
| T8 | Un-advertised server blocked (ping OK, UDP 7000 dropped) | SDN controls service traffic |
| T9 | Killed server expires + gets blocked | timeout / failure detection (D2 preview) |

## Troubleshooting
* `ERROR: switch could not reach the SDN controller` -> start Terminal 1 first.
* Odd errors after a crash -> `sudo mn -c`.
* If `sudo python3 topo/topology.py --test --datapath user` is needed: only when your VM has no
  Open vSwitch kernel module (normally not needed).
