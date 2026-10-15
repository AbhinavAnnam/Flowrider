# Phase 2: Mininet and OS-Ken integration

## Status and inspection

**PARTIALLY COMPLETE:** implementation and unprivileged verification are present;
live Mininet validation requires a sudo password unavailable to the agent.
Do not interpret simulated peers or mock datapaths as successful live OVS/host tests.
The recorded outputs are in [phase2-validation.txt](phase2-validation.txt).

Before this change, the repository contained the independent NetworkX/Random
Forest simulation and an untracked local `controller.py` implementing an OS-Ken
learning switch and working programmatic launcher. There were no Mininet scripts
or tests. The simulation graph already had the requested six routers and seven
links. The earlier `mn --topo single,2` lab had one switch, so it did not match
that graph. The new runner implements the intended graph without replacing a
running lab. The previous controller's launcher, table-miss, PacketIn,
MAC learning and forwarding behavior were extended for this fixed network.

Observed environment:

| Component | Version / interpreter |
| --- | --- |
| WSL Ubuntu | 24.04.3 LTS |
| OS-Ken runtime | Python 3.10.16, `~/.pyenv/versions/3.10.16/bin/python` |
| OS-Ken | 4.2.2; native threading hub; no console entry points |
| Mininet | 2.3.0, imported with `/usr/bin/python3` |
| Ubuntu Python | 3.12.3 |
| Open vSwitch | 3.3.9 |
| NetworkX in OS-Ken Python | 3.4.2 |
| Protocol used | OpenFlow 1.3 only (`OFP_VERSION = 4`) |

NetworkX was the only dependency installed during implementation. The existing
virtual environment was not used or changed. No Ryu, Ubuntu OS-Ken package, new
virtual environment, or interpreter changes were introduced. Other OVS protocol
versions were not exhaustively tested. An existing `s1` management endpoint on
TCP 6654 reported OpenFlow 1.3 and only its LOCAL port; it was not deleted.

## Architecture and packet path

```mermaid
flowchart LR
    H1[h1] --- S1[s1 / R1]
    S1 --- S2[s2 / R2]
    S2 --- S3[s3 / R3]
    S3 --- S4[s4 / R4]
    S4 --- S5[s5 / R5]
    S5 --- S6[s6 / R6]
    S6 --- S1
    S2 ---|backbone| S5
    S4 --- H4[h4]
    C[OS-Ken controller] -. OpenFlow 1.3 TCP 6653 .- S1
    C -. control connections from all six switches .- S4
```

Each switch also has one host, h1 through h6. All six switches initiate their
control connections to the controller. The diagram shows two control connections
for readability; the controller does not carry host traffic on TCP 6653.

Packet path: **Host → OVS → OpenFlow controller decision → OVS forwarding →
destination host.** A table miss sends the full Ethernet frame to OS-Ken.
The controller computes the source-to-destination path using Dijkstra and static
`base_delay` weights, verifies the ingress port, and sends a PacketOut with an
explicit output port. It installs a matching unicast FlowMod at that switch.
The next switch repeats this process until the path is learned. Subsequent
matching packets use OVS flow entries directly.

The original simulation functions remain public and independent of OS-Ken and
Mininet. Only their shared link definitions moved to `topology_config.py`, and
Matplotlib is imported when visualization is requested. The Random Forest model,
orchestration, and equation `W = D₀ × (1 + 25 × Û⁴)` are unchanged.

## Wiring and link parameters

`topology_config.py` is the single source of link/host definitions. DPID `i`
maps to `s<i>`, logical `R<i>`, and simulation `Router_<i>`. DPIDs are encoded
as 16 hexadecimal digits: s6 is `0000000000000006`.

Every host hN uses `10.0.0.N/24`, MAC `00:00:00:00:00:0N`, host port 0,
and switch port 1. All hosts share one IPv4 broadcast domain.

| Link | Endpoint ports | Logical capacity | Default Mininet rate | Delay |
| --- | --- | --- | --- | --- |
| R1–R2 | s1:2 ↔ s2:2 | 1000 Mbps | 100 Mbps | 5 ms |
| R2–R3 | s2:3 ↔ s3:2 | 1000 Mbps | 100 Mbps | 5 ms |
| R3–R4 | s3:3 ↔ s4:2 | 1000 Mbps | 100 Mbps | 5 ms |
| R4–R5 | s4:3 ↔ s5:2 | 1000 Mbps | 100 Mbps | 5 ms |
| R5–R6 | s5:3 ↔ s6:2 | 1000 Mbps | 100 Mbps | 5 ms |
| R6–R1 | s6:3 ↔ s1:3 | 1000 Mbps | 100 Mbps | 5 ms |
| R2–R5 | s2:4 ↔ s5:4 | 2000 Mbps | 200 Mbps | 2 ms |

Mininet's installed `TCIntf.bwParamMax` is 1000 Mbps. Passing `bw=2000`
would silently leave the intended backbone shaping unapplied after a warning.
The default `--bandwidth-scale 0.1` keeps the 2:1 capacity ratio; scales must be
positive and no greater than 0.5. Host links use 100 Mbps at the default scale.
The simulation retains its original unscaled capacities. This is a lab scaling
choice, not a claim of achievable WSL throughput.

`TCLink` applies HTB rate classes and netem delays. The runner reads actual
`tc class show` and `tc qdisc show` output on every switch-facing endpoint and
fails startup if the requested rate/delay is absent. This checks applied kernel
configuration, not measured throughput. Delay is applied at each endpoint's
egress, so one traversal of an inter-switch link adds its configured delay;
a round trip traverses both directions. Host access links have no added delay.

Inspect the configuration without privileges:

```bash
cd ~/Flowrider
/usr/bin/python3 mininet_topology.py --describe
```

## Controller behavior

- `datapaths` tracks current switch connections. Unknown DPIDs are rejected.
  Disconnections remove switch/port availability and invalidate routes. Events
  from replaced connections cannot modify the new connection's state.
- OS-Ken's built-in OFPHandler performs negotiation and requests the initial
  port descriptions. The controller collects multipart replies and validates
  exact interface names and expected port numbers before enabling forwarding.
- `TopologyState.graph` is a NetworkX graph keyed by DPIDs. Nodes have OVS and
  logical-router names. Edges retain capacities, `base_delay`, `latency`,
  endpoint `ports`, `configured`, and `active`. `active_graph()` excludes offline
  switches and inactive links rather than routing through infinite-weight edges.
- Topology is **statically configured**. `link_ports[(dpid, port)]` maps both
  directions of every cable. A configured edge becomes available only when
  both connected endpoints report matching, operational ports. A PortStatus
  event records endpoint state; it does **not** prove the identity or reachability
  of the remote peer. There is no LLDP discovery or silent-path probing.
- `hosts` pre-registers the six MAC attachments so ARP and early unicast have
  deterministic destinations. Additional source MACs can be learned only on
  configured access ports. Static host moves are rejected. Transit ports never
  become host attachments. Learned MACs do not expire in this small lab.
- Unknown unicast, multicast and broadcast use a deterministic shortest-path
  tree rooted at the source attachment. At each switch, only frames arriving
  from the tree parent (or root host port) are accepted; output goes to explicit
  child/access ports. Broadcast rules are not cached. No `OFPP_FLOOD` or
  `OFPP_NORMAL` actions are used. ARP travels through the actual tree links.
- Known unicast rules match ingress, source MAC and destination MAC. They use
  priority 10, idle timeout 30 s, hard timeout 120 s, and owner cookie
  `0x4652000000000000`. Each PacketOut handles the triggering frame; buffered
  packets omit redundant packet data. Traffic stays on the computed path.
- Switch, port, or learned-host changes delete **all FlowRider-owned routes**
  across current healthy datapaths, using a cookie mask. Table-miss rules remain.
  New PacketIn forwarding pauses until deletion barriers complete. Old barrier
  replies cannot release a later update. Packets can be lost during convergence;
  hosts retry. This is basic recovery, not hitless failover or atomic updates
  across multiple OVS instances. Restoration recomputes paths on subsequent traffic.
- No-path/off-path unicast is dropped with a warning. OpenFlow errors log
  type/code/data, remove the failed switch from routing, and close its connection.
  A missing barrier reply deliberately keeps forwarding paused until the switch
  disconnects/reconnects; there is no separate barrier timeout worker.

## Startup and shutdown

The controller's existing Python already has OS-Ken. If dependencies are missing:

```bash
~/.pyenv/versions/3.10.16/bin/python -m pip install -r requirements-controller.txt
```

Terminal 1:

```bash
cd ~/Flowrider
~/.pyenv/versions/3.10.16/bin/python -u controller.py \
  --ofp-listen-host 127.0.0.1 --ofp-tcp-listen-port 6653
```

Terminal 2:

```bash
cd ~/Flowrider
sudo /usr/bin/python3 mininet_topology.py \
  --controller-ip 127.0.0.1 --controller-port 6653 --bandwidth-scale 0.1
```

Look for `Switch connected`, `Validated static port map` for DPIDs 1–6, and
`Forwarding ready` with six switches and seven active links. Early messages may
show fewer links as switches initialize. The runner checks controller TCP access,
waits up to 20 seconds for switch connections, and verifies TC configuration.
It leaves ARP dynamic rather than pre-populating neighbor caches.

At `mininet>`:

```text
pingall
h1 ping -c 3 h4
h2 ip -4 neigh show
sh ovs-ofctl -O OpenFlow13 dump-flows s2
sh ovs-vsctl show
link s2 s5 down
h2 ping -c 5 h5
sh ovs-ofctl -O OpenFlow13 dump-flows s2
link s2 s5 up
h2 ping -c 5 h5
exit
```

h2→h5 normally uses s2 port 4 (2 ms backbone). While that link is down, it must
take a ring path (15 ms). A few packets may be lost as PortStatus and deletion
barriers are processed. ARP should recover after neighbor caches are flushed too.

`exit` stops only this runner's Mininet objects in `finally`. Startup failures
also enter that cleanup path. Then stop the controller with Ctrl-C. Do not use
global `mn -c` while other networks exist. A forced process/WSL termination can
leave stale OVS objects; inspect their ownership before removing anything.

## Testing and evidence

Ordinary tests (no root, real OS-Ken parser/encoder plus mock datapath transport):

```bash
~/.pyenv/versions/3.10.16/bin/python -m unittest discover -s tests -v
```

This includes a real controller subprocess on an isolated ephemeral TCP port and
six simulated switch peers. It validates real handshakes and PacketOut decisions
before/after synthetic failure and recovery. It is explicitly not a Mininet test.
Five Mininet structure/safety tests skip here because Ubuntu's Mininet is not installed
in the pyenv interpreter. Run them separately without root:

```bash
/usr/bin/python3 -m unittest discover -s tests -p test_mininet_topology.py -v
```

The topology tests cover all 128 physical-link failure combinations for all six
source roots, checking tree traversal and component reachability without duplicate
switch visits. Other tests cover registration/disconnection, port mappings, host
learning, weights, no-path behavior, timeouts/cookies, buffered PacketOut, protocol
errors, multipart descriptions, stale barriers, failure and recovery.

Live test: start Terminal 1's controller, stop the interactive lab, then run:

```bash
cd ~/Flowrider
set -o pipefail
sudo /usr/bin/python3 tests/integration_phase2.py --repeat 2 2>&1 | tee /tmp/flowrider-live.log
```

This creates the topology, verifies actual TC configuration, all six OpenFlow
connections/DPIDs, dynamic ARP, `pingall`, no duplicate ping replies, installed
unicast rules with packet hits, backbone rerouting, restoration, an isolated
host/no-path case, ARP recovery, scoped cleanup and repeated execution. It exits
nonzero on failure. Keep the controller log alongside the test output. Ping-based
loop checks are bounded observations; the exhaustive unit tree tests provide the
structural proof for the configured static graph.

## Troubleshooting and limitations

- **`osken-manager` missing:** expected. OS-Ken removed its CLI in 4.0.0. The
  installed package has no runnable launcher module. Use `controller.py` and its
  `AppManager.run_apps()` startup block; no Ryu or Eventlet monkey-patching is needed.
- **Wrong Python / `No module named os_ken`:** use the explicit pyenv 3.10 path.
  Use `/usr/bin/python3` for the topology/tests importing Ubuntu's Mininet.
- **Existing `s1`–`s6`:** the runner refuses to let OVS replace those bridges.
  Stop their owning lab first. Inspect `sudo ovs-vsctl show` and running Mininet
  sessions. A stale bridge may remain after its session ends; remove only bridges
  you have identified as your stopped lab's objects. No cleanup is automated globally.
- **No controller connection:** inspect `ss -ltnp '( sport = :6653 )'`, OVS
  controller targets and `is_connected`. TCP 6653 is the controller listener;
  an old `ptcp:6654` is a switch management listener, not another required controller.
- **Invalid static wiring:** use this repository's runner and inspect
  `ovs-ofctl -O OpenFlow13 show sN`. The old single-switch/two-host topology is
  intentionally unsupported by this fixed six-switch configuration. Reconnect
  after correcting a missing/unexpected port map.
- **Ping loss:** inspect controller errors, pending startup, active links, flow
  dumps and ARP. Bring both endpoints of a failed cable back up. Check actual
  `tc` output; never infer shaping success from link arguments alone.
- Only the configured IPv4 Ethernet/ARP lab is supported. IPv6/LLDP PacketIn is
  ignored and Mininet hosts have IPv6 disabled. There is no VLAN routing, DHCP,
  IP subnet routing, multi-controller coordination, arbitrary topology discovery,
  authenticated host learning, silent-link failure detection or production HA.
- Live Mininet execution, packet delivery, bandwidth/delay application and repeated
  cleanup remain unverified in this agent session. Root access is required.
- The full graphical/ML simulation was not rerun because its optional scientific
  dependencies are absent from the controller Python. Its shared graph/path API
  is exercised; `main.py` and `traffic_engine.py` are unchanged.

## Completion checklist and Phase 3 boundary

| Requirement | Implemented | Verification in this session |
| --- | --- | --- |
| Six switches, seven links, six hosts and deterministic ports | Yes | Mininet structure + shared graph tests |
| OS-Ken startup and TCP 6653 | Yes | Real listener and OpenFlow 1.3 Hello |
| Switch registration, disconnection, protocol messages | Yes | Unit + real TCP with simulated peers |
| Static topology, both-ended state, hosts and paths | Yes | Unit tests |
| ARP/broadcast loop prevention | Yes | Exhaustive tree tests; live ARP pending |
| Unicast FlowMods/PacketOut and invalidation | Yes | Real encoding + simulated wire peers; live OVS pending |
| Failure, no path and recovery | Yes | Unit + synthetic wire events; live cable events pending |
| Applied bandwidth/delay validation | Yes | Parsing/config tests; actual kernel settings pending |
| Live ping, ARP, counters, repeated cleanup | Test provided | Not run: sudo password required |
| Reproducible instructions and limitations | Yes | Commands checked where unprivileged |

Phase 2 can be called **COMPLETE** only after successful live test output is
captured. It is not blocked from further useful work: implementation and all
available unprivileged verification are complete.

Phase 3 can reuse `datapaths`, `TopologyState.graph`, `link_ports`, `hosts`,
`active_graph()`, `path()`, and cookie-scoped route invalidation. Routing defaults
to `routing_weight = 'base_delay'`; future work can populate a separate edge
weight and select that attribute, then explicitly invalidate stale rules. Future
telemetry-driven edge weights and prediction will need their own tests and update
policy. No telemetry collection, Random Forest integration, forecasting, frontend,
or topology snapshot retraining runs in this controller.

## Primary references

- [OS-Ken CLI removal release notes](https://docs.openstack.org/releasenotes/os-ken/2026.1.html)
- [OS-Ken OpenFlow 1.3 messages](https://docs.openstack.org/os-ken/latest/ofproto_v1_3_ref.html)
- [Mininet TCIntf bandwidth/delay configuration](https://mininet.org/api/classmininet_1_1link_1_1TCIntf.html)

The installed 4.2.2 `app_manager.py`, `ofp_handler.py`, OpenFlow encoders, native
hub, and Ubuntu Mininet `link.py`/`node.py` were also inspected directly; installed
behavior takes precedence over older manager-launch examples in online docs.
