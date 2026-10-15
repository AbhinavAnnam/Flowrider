# Phase 2: Mininet and OS-Ken integration

## Status and inspection

**PARTIALLY COMPLETE:** implementation and historical unprivileged verification
are present. The original agent implementation session could not run live
Mininet because sudo required a password. The user subsequently reported manual
backbone-failover observations from October 15, 2026; these are recorded separately
in the step-by-step guide below. Automated live integration success is not yet
evidenced. Do not interpret simulated peers or mock datapaths as successful live
OVS/host tests. The original outputs remain in
[phase2-validation.txt](phase2-validation.txt); they were not regenerated during
this documentation-only update.

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
  cleanup were unverified in the original agent implementation session. The later
  user-reported manual failover observations are recorded below; they do not
  establish that every live check or the automated integration suite passed.
- The full graphical/ML simulation was not rerun because its optional scientific
  dependencies are absent from the controller Python. Its shared graph/path API
  is exercised; `main.py` and `traffic_engine.py` are unchanged.

## Completion checklist and Phase 3 boundary

The following table preserves the original implementation-session record. See
the historical manual record below for the subsequently supplied observations.

| Requirement | Implemented | Verification in the original implementation session |
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

Phase 2 can be called **COMPLETE** only after the manual and automated live
acceptance evidence described below is captured. The implementation and original
unprivileged verification provide a basis for that remaining validation.

Phase 3 can reuse `datapaths`, `TopologyState.graph`, `link_ports`, `hosts`,
`active_graph()`, `path()`, and cookie-scoped route invalidation. Routing defaults
to `routing_weight = 'base_delay'`; future work can populate a separate edge
weight and select that attribute, then explicitly invalidate stale rules. Future
telemetry-driven edge weights and prediction will need their own tests and update
policy. No telemetry collection, Random Forest integration, forecasting, frontend,
or topology snapshot retraining runs in this controller.

## Phase 2 — Step-by-Step Testing and Verification Guide

This guide is a procedure to execute and record, not a claim that all its tests
have already passed. Run the numbered manual tests in order. Commands in a
`bash` block below are for a normal Ubuntu shell unless the text specifies the
`mininet>` prompt; commands beginning with `sh` are entered at that prompt and
run a shell command from Mininet. Do not type the prompt itself.

### Environment and permissions

Use the Ubuntu/WSL environment and versions listed near the beginning of this
document. **FlowRider uses OS-Ken, not Ryu.** Its controller speaks OpenFlow 1.3
to Open vSwitch; it does not use the Mininet default controller.

| Part | Requirement and reason |
| --- | --- |
| Controller | Existing Python 3.10.16 at `~/.pyenv/versions/3.10.16/bin/python`, with OS-Ken 4.2.2 and NetworkX. See [requirements-controller.txt](../requirements-controller.txt). |
| Mininet | Ubuntu's `/usr/bin/python3`, with the system Mininet package. Do not assume the pyenv Python can import Mininet. |
| OVS and traffic control | Running Open vSwitch, `ovs-vsctl`, `ovs-ofctl`, `tc`, and `ip`; the runner uses kernel interfaces, HTB, and netem. |
| Privileges | The controller needs no sudo on port 6653. Mininet creation, OVS administration and live tests require permitted sudo/root access. |
| Wiring | Use [mininet_topology.py](../mininet_topology.py), which implements [topology_config.py](../topology_config.py). The older `single,2` topology does not match the controller's static port map. |

Optional read-only environment checks, in a shell before startup:

```bash
cd ~/Flowrider
~/.pyenv/versions/3.10.16/bin/python --version
~/.pyenv/versions/3.10.16/bin/python -m pip show os-ken networkx
/usr/bin/python3 --version
/usr/bin/python3 -c 'import mininet; print(mininet.__file__)'
mn --version
ovs-vsctl --version
command -v ovs-ofctl tc ip
sudo -l
```

These checks inspect dependencies and permissions; they do not install anything.
If an import fails, first check which interpreter you used. Do not install Ryu,
replace Python, or create a new environment to work around an interpreter mix-up.

### Before startup: inspect existing networks

In Terminal 2, before creating the lab:

```bash
sudo ovs-vsctl list-br
sudo ovs-vsctl show
ps -eo user,pid,args | rg 'mininet|controller.py'
```

Save the bridge list as your baseline. Unrelated bridges may legitimately exist.
The FlowRider runner refuses existing `s1`–`s6` names and matching host/switch
interfaces rather than replacing them. An `s1` bridge alone does not prove that
it is stale or belongs to FlowRider. Inspect its ports, controller and owning
session before considering removal. If its Mininet session is still running,
exit that session normally. Do not blindly delete bridges or run global cleanup.

### Terminal 1 — Start OS-Ken controller

```bash
cd ~/Flowrider
~/.pyenv/versions/3.10.16/bin/python -u controller.py \
  --ofp-listen-host 127.0.0.1 \
  --ofp-tcp-listen-port 6653
```

Keep this terminal open. OS-Ken should log loading/instantiating the controller
application and `os_ken.controller.ofp_handler`. No switch-connection messages
are required until Mininet starts. The startup block in
[controller.py](../controller.py) uses `AppManager.run_apps()`. Use the checked-in
startup command above; this OS-Ken installation does not provide `osken-manager`.

For evidence capture, the same command can redirect both output streams through
`tee /tmp/flowrider-controller.log` using `2>&1 | tee /tmp/flowrider-controller.log`.
Choose a new log filename for each run if earlier results must be retained.

### Terminal 2 — Start Mininet

```bash
cd ~/Flowrider
sudo /usr/bin/python3 mininet_topology.py
```

The default controller target is `127.0.0.1:6653` and the bandwidth scale is 0.1.
The runner creates six OVS switches and six hosts, waits for controller
connections, and checks actual TC configuration before opening `mininet>`.
Terminal 1 should report six validated switches and seven active links after
initialization. Partial switch/link counts while startup is in progress are normal.

### Manual Test 1 — Controller connectivity

**Purpose:** establish that all six configured switches are registered and using
OpenFlow 1.3 before testing host packets.

**Where/commands:** inspect Terminal 1 for `Switch connected` and
`Validated static port map` messages for DPIDs 1 through 6. The final ready
message should include this exact substring:

```text
Forwarding ready: switches=[1, 2, 3, 4, 5, 6] active_links=7
```

In Terminal 2, at `mininet>`:

```text
sh ovs-vsctl show
sh ovs-vsctl --columns=target,is_connected list Controller
sh for switch in s1 s2 s3 s4 s5 s6; do ovs-ofctl -O OpenFlow13 show "$switch"; done
```

**Expected/pass:** six lab bridges, connected controller targets on port 6653,
the six-switch/seven-link ready message, and `(OF1.3)` features replies with
DPIDs `0000000000000001` through `0000000000000006`. Inspect the lab's controller
entries; unrelated controller rows may appear if other bridges exist.
**Fail:** missing switches, rejected port maps, protocol errors, or persistent
`is_connected=false`. Common causes are the wrong topology, controller target,
interpreter, OVS service state, or OpenFlow version. Port state supports the
configured wiring; this is not independent discovery of remote cable identities.

### Manual Test 2 — Host connectivity

**Purpose:** exercise IPv4 forwarding and dynamic ARP across the whole lab.

**Where/command:** Terminal 2, at `mininet>`:

```text
pingall
```

**Expected/pass:** the summary reports `0% dropped` (normally `30/30 received`
for six hosts). Each host should reach the other five. **Fail:** packet loss
after initialization or repeated failures between particular hosts. Common causes
are incomplete startup, invalid host/port mappings, down links, OpenFlow errors,
or failed ARP. If you tested during convergence, preserve that result, wait for
the ready message, and rerun; do not report the first run as loss-free.

For ARP evidence, also inspect:

```text
h2 ip -4 neigh show
```

After successful pings, remote host entries should have `lladdr` MAC addresses
rather than remain `INCOMPLETE` or `FAILED`.

### Manual Test 3 — OpenFlow forwarding rules

**Purpose:** verify that the controller installs unicast rules and that later
packets actually hit them.

**Where/commands:** Terminal 2, at `mininet>`. Generate traffic, then inspect
immediately:

```text
h1 ping -c 5 h4
sh ovs-ofctl -O OpenFlow13 dump-flows s1
```

**Expected/pass:** a priority-0 table-miss rule with
`actions=CONTROLLER:65535`, plus priority-10 FlowRider unicast rules with explicit
output ports. At least a relevant unicast rule should show increasing/nonzero
packet counters after repeated traffic. The first packet may be forwarded by
PacketOut and therefore does not itself establish a rule-counter hit.

| Flow field | What to look for and what it means |
| --- | --- |
| `priority` | 10 for learned unicast; 0 for the catch-all table miss. The more specific priority-10 rule wins when its match applies. |
| `in_port` | The switch-local ingress port; port 1 is the host access port in this lab. |
| `dl_src` | Ethernet source MAC (`eth_src` in controller code). |
| `dl_dst` | Ethernet destination MAC (`eth_dst` in controller code). |
| `actions` | `output:N` selects a physical output port. Map it to `sN-ethP` using the wiring table or `ovs-ofctl show`. |
| `n_packets` | Number of packets matched by that rule, not all packets handled by the controller or switch. |
| `idle_timeout` | Unicast rules expire after 30 seconds without matching traffic. |
| `hard_timeout` | Unicast rules expire after 120 seconds even if traffic continues. |
| `cookie` | `0x4652000000000000` identifies FlowRider-owned unicast routes for scoped deletion. It is not authentication. The table-miss rule uses the default cookie 0. |

**Fail/common causes:** no table miss indicates configuration/connection trouble;
no unicast rules may simply mean they expired or no known-destination traffic
was generated. Regenerate pings and inspect within 30 seconds. A rule that stays
at zero hits needs more matching packets or a check that you inspected the
correct direction/MAC pair. A route pointing to a failed or unexpected port
requires controller-log and port-state inspection.

### Manual Test 4 — Simulate backbone link failure

**Purpose:** check that the configured backbone becomes unavailable and old
routes are invalidated.

**Where/command:** Terminal 2, at `mininet>`:

```text
link s2 s5 down
```

**Expected/pass:** Terminal 1 reports port 4 down on DPID 2 and DPID 5, logs
`Topology changed; invalidating routes`, and eventually reports six active links
with all six switches still registered. The two endpoint events need not arrive
together; the first down endpoint already makes this configured edge inactive.
**Fail:** persistent active-link count 7 or forwarding through the disabled
backbone after convergence. Common causes are disabling a different link,
missing port events, rejected wiring, or watching the wrong controller process.

If needed, inspect the actual endpoint states at `mininet>`:

```text
sh ip link show dev s2-eth4
sh ip link show dev s5-eth4
sh ovs-ofctl -O OpenFlow13 show s2
sh ovs-ofctl -O OpenFlow13 show s5
```

### Manual Test 5 — Verify alternative connectivity

**Purpose:** prove that traffic still reaches h5 while the direct backbone is down,
and inspect s2's alternative forwarding action.

**Where/commands:** after Terminal 1 settles at six active links, Terminal 2,
at `mininet>`:

```text
h2 ping -c 5 h5
sh ovs-ofctl -O OpenFlow13 dump-flows s2
```

If the relevant rule expired before inspection, generate more traffic and inspect
again immediately:

```text
h2 ping -c 10 h5
sh ovs-ofctl -O OpenFlow13 dump-flows s2
```

**Expected/pass:** every packet in the settled five- or ten-packet ping run is
received, with `0% packet loss` and no duplicate replies. Find the forward rule
with `in_port=1`, source MAC `00:00:00:00:00:02`, destination MAC
`00:00:00:00:00:05`, priority 10, and the FlowRider cookie. Its output must be a
live ring port (2 or 3), not failed port 4; repeated traffic should give it
positive packet hits. Equal-cost ring alternatives exist, so a complete path
must not be inferred from a tie-breaking assumption.

**Historical observation:** the user's October 15, 2026 manual run achieved
0% loss for h2→h5; the forward rule used **s2-eth2 (`output:2`)** instead of
**s2-eth4 (`output:4`)**, and priority-10 FlowRider rules processed packets.
This verifies alternative forwarding at s2, but does **not** independently trace
the entire multi-switch packet path. See the historical record below.

**Fail/common causes:** sustained loss, duplicate replies, a stale output-4 rule,
or absent packet hits after repeated matching traffic. Check the down-port logs,
deletion barriers, remaining ring links, ARP and the direction of the inspected
rule. Brief convergence loss must be recorded; rerun only after the controller
reports readiness to assess steady-state connectivity.

### Manual Test 6 — Restore backbone

**Purpose:** verify recovery and invalidate routes selected during the failure.

**Where/commands:** Terminal 2, at `mininet>`:

```text
link s2 s5 up
```

Wait for Terminal 1 to report restored endpoint ports, route invalidation and a
ready state with seven active links. Then at `mininet>`:

```text
pingall
h2 ping -c 5 h5
sh ovs-ofctl -O OpenFlow13 dump-flows s2
```

**Expected/pass:** active links increase from 6 to 7, `pingall` reports
`0% dropped`, and renewed h2→h5 traffic can use s2 port 4, the lowest-delay
backbone route. **Fail/common causes:** only one endpoint recovered, the active
count stays at 6, stale forwarding persists, or hosts remain unreachable. Inspect
both port states and controller errors; remember that restoration is not hitless.

### Manual Test 7 — Traffic shaping verification

**Purpose:** inspect kernel traffic-control configuration on the restored backbone.

**Where/commands:** Terminal 2, at `mininet>`:

```text
sh tc qdisc show dev s2-eth4
sh tc class show dev s2-eth4
```

**Expected/pass at default scale 0.1:** qdisc output includes HTB scheduling and
netem delay `2ms`; the HTB class has `rate 200Mbit` and `ceil 200Mbit`.
Repeat on `s5-eth4` to inspect the other direction:

```text
sh tc qdisc show dev s5-eth4
sh tc class show dev s5-eth4
```

**Fail/common causes:** missing HTB/netem, incorrect rate/delay, or a nonexistent
interface. Check the runner's `Applied TC` output, kernel/tool support, interface
mapping and any non-default `--bandwidth-scale`. The runner checks applied rate
and delay at startup; the manual inspection also checks the displayed ceiling.
These commands verify **configured shaping**, not measured throughput or
end-to-end latency. The model's 2000 Mbps backbone is deliberately emulated at
200 Mbps by default; do not treat that scaling as an error.

### Manual Test 8 — Clean shutdown

**Purpose:** check that stopping this lab removes its own bridges without
disturbing unrelated networks.

**Where/command:** Terminal 2, at `mininet>`:

```text
exit
```

After the normal shell prompt returns in Terminal 2:

```bash
sudo ovs-vsctl list-br
```

**Expected/pass:** the lab's `s1`–`s6` bridges are absent, while unrelated bridges
from your saved baseline remain. Terminal 1 should receive switch disconnections;
keep the controller running for the restart test. **Fail/common causes:** owned
bridges remain, shutdown raised an exception, or the process was forcibly killed.
Inspect `sudo ovs-vsctl show`, process ownership and remaining interfaces before
any cleanup. An empty global bridge list is not required when other networks exist.

### Manual Test 9 — Restart verification and final shutdown

**Purpose:** verify that cleanup permits a fresh network to reconnect to the
same running controller.

**Where/command:** Terminal 2's shell, leaving Terminal 1 running:

```bash
cd ~/Flowrider
sudo /usr/bin/python3 mininet_topology.py
```

After the six-switch/seven-link ready state returns, at `mininet>`:

```text
pingall
```

**Expected/pass:** startup has no bridge-name collisions, all six switches
reconnect, and `pingall` reports `0% dropped`. **Fail/common causes:** stale
objects from the first run, a stopped controller, wrong target parameters, or
state/port validation errors after reconnect.

For final shutdown, enter `exit` at `mininet>`, check `sudo ovs-vsctl list-br`
again in Terminal 2, then press **Ctrl-C in Terminal 1**. You can inspect
`ss -ltnp '( sport = :6653 )'` from the shell to confirm your controller listener
has stopped. If some other process owns that port, identify it rather than
terminating it blindly.

### Automated verification procedure

Run from `~/Flowrider`. The commands below are instructions for the next test run;
**none of these tests was rerun during this documentation-only update**.

#### Unit tests: topology and controller with mocked transport

In a normal shell, without sudo:

```bash
cd ~/Flowrider
~/.pyenv/versions/3.10.16/bin/python -m unittest discover -s tests -p test_topology_state.py -v
~/.pyenv/versions/3.10.16/bin/python -m unittest discover -s tests -p test_controller.py -v
/usr/bin/python3 -m unittest discover -s tests -p test_mininet_topology.py -v
```

The topology tests use a real NetworkX graph. Controller tests use OS-Ken's real
message encoders with mocked datapath transport. Mininet structure/safety tests
inspect topology objects and mock privileged startup/preflight paths; they do
not create a live network. Expected success is unittest's final `OK`, with no
unexpected failures or skips. Missing imports commonly mean the wrong Python.

#### Controller socket test: real process, simulated OpenFlow peers

In a normal shell, without sudo:

```bash
~/.pyenv/versions/3.10.16/bin/python -m unittest discover -s tests -p test_openflow_socket.py -v
```

This starts a temporary controller subprocess on an ephemeral local TCP port,
connects six simulated OpenFlow 1.3 peers, and checks PacketOut decisions before
failure, after failure, and after restoration. The test terminates its own
subprocess. Expected result is `OK`; a timeout or assertion failure is a failed
test. It does not exercise real OVS forwarding, Mininet hosts, or kernel shaping.

For a combined pyenv run, the existing command is:

```bash
~/.pyenv/versions/3.10.16/bin/python -m unittest discover -s tests -v
```

This includes the socket test, so it is not a purely mocked/unit run. The five
Ubuntu Mininet structure/safety tests skip in that interpreter; run them with
`/usr/bin/python3` as shown above. Do not count those skips as passing tests.

#### Real Mininet integration test

Start the controller in Terminal 1. In Terminal 2, exit any interactive FlowRider
lab first; this test creates and stops its own network and needs the same names.
Then run:

```bash
cd ~/Flowrider
sudo /usr/bin/python3 tests/integration_phase2.py --repeat 2
```

The script's actual arguments support `--repeat`, `--controller-ip`,
`--controller-port`, and `--bandwidth-scale`. It expects an external controller;
it does not start that controller. Each of the two runs checks real Mininet
startup, six OpenFlow 1.3 DPIDs, TC configuration, ARP, `pingall`, FlowRider rules
with packet hits, backbone failure/rerouting/restoration, isolation/no-path
behavior and ARP recovery. Scoped shutdown and another preflight check verify
cleanup/readiness for the next run.

**Pass:** both iterations finish, the process exits 0, and the final output is:

```text
*** LIVE PHASE 2 PASSED, INCLUDING CLEANUP AND REPEATED EXECUTION
```

An individual run's `LIVE PHASE 2 NETWORK CHECKS PASSED` line does not by itself
prove that both runs and cleanup completed. **Fail:** nonzero exit, an exception,
timeout, loss after convergence, missing rules/counters, or failed cleanup.
Read the first failing check and controller logs before retrying. A sudo denial
means the network test did not execute, not that it passed or found a network bug.

To retain output and the Python process's exit status in Bash:

```bash
set -o pipefail
sudo /usr/bin/python3 tests/integration_phase2.py --repeat 2 2>&1 | tee /tmp/flowrider-live.log
echo "Live test exit status: $?"
```

Keep Terminal 1's controller log as well. After the live test returns, inspect
the remaining bridges and stop the controller with Ctrl-C. Do not run manual
link commands against the automated test's network while it is executing.

### Troubleshooting: diagnose before changing anything

Use the indicated shell or prompt. These are diagnostic commands; avoid global
cleanup, broad process termination, dependency installation, or service restarts
as a first response.

| Issue | Safe diagnostics and interpretation |
| --- | --- |
| Stale-looking `s1` or startup name collision | Shell: `sudo ovs-vsctl list-br`, `sudo ovs-vsctl show`, `sudo ovs-vsctl list-ports s1`, and `ps -eo user,pid,args \| rg 'mininet\|controller.py'`. Identify the owner and active session first. Prefer normal `exit` in that session. Remove only objects confirmed to belong to your stopped lab; do not blindly delete bridges. |
| Controller not listening on 6653 | Shell: `ss -ltnp '( sport = :6653 )'` and inspect Terminal 1's startup errors. Confirm the explicit pyenv interpreter and bind options; identify any existing listener before taking action. |
| Switches not connecting | At `mininet>`: `sh ovs-vsctl show` and `sh ovs-vsctl --columns=target,is_connected list Controller`. Confirm lab targets are `tcp:127.0.0.1:6653`. Shell: `systemctl is-active openvswitch-switch` can inspect OVS service state on systemd-enabled WSL; otherwise inspect the service manager's error and OVS processes. Do not confuse old `ptcp:6654` management listeners with the controller. |
| OpenFlow version mismatch | At `mininet>`: `sh ovs-vsctl get Bridge s1 protocols` and `sh ovs-ofctl -O OpenFlow13 show s1`. Expect `OpenFlow13` and `(OF1.3)`; repeat for affected switches. Compare the runner's switch settings and controller's `OFP_VERSIONS` before changing configuration. |
| Missing sudo privileges | Shell: `id` and `sudo -l`. A denial prevents live Mininet/OVS administration; use an authorized administrator or permitted account. Controller/unit tests are separate from root-only integration. Do not change permissions globally to bypass the restriction. |
| `pingall` loss | Wait for the full ready message, then inspect `h2 ip -4 neigh show`, `sh ovs-ofctl -O OpenFlow13 dump-flows s2`, and `sh ovs-ofctl -O OpenFlow13 show s2` at `mininet>`. Check logs, ARP, both link endpoints and whether the test ran during invalidation barriers. Record transient loss separately from a later clean rerun. |
| Flow rules disappear | At `mininet>`: run `h2 ping -c 10 h5`, then immediately `sh ovs-ofctl -O OpenFlow13 dump-flows s2`. Idle expiry is 30 s and hard expiry is 120 s; topology changes also delete routes. Check direction/MAC matches before treating an absent rule as failure. |
| Link failure not detected | At `mininet>`: `sh ip link show dev s2-eth4`, `sh ip link show dev s5-eth4`, and `sh ovs-ofctl -O OpenFlow13 show s2`/`s5`. Compare the two DPID/port-4 logs. The supported test is `link s2 s5 down`; a silent remote fault without port-state change is outside current detection support. |
| Cleanup or restart problems | After `exit`, shell: `sudo ovs-vsctl show`, `ip -brief link show`, and the process inspection above. Compare with the saved baseline and the runner's shutdown errors. Preserve unrelated networks; do not use destructive global cleanup. |
| Different Python environments | Shell: `~/.pyenv/versions/3.10.16/bin/python -m pip show os-ken networkx` and `/usr/bin/python3 -c 'import mininet; print(mininet.__file__)'`. A successful import in one interpreter says nothing about the other. Use the exact interpreter paths in this guide. |
| `traceroute` missing or unhelpful | At `mininet>`: `h2 which traceroute` checks availability without installation. Ordinary IP traceroute reveals Layer 3 TTL-expiring routers, not each Layer 2 OVS switch in this shared subnet. It cannot independently prove the ring path. Correlate the matching forward rule and counters on each candidate switch if a full switch path must be established. |

### Historical evidence — October 15, 2026

These records have different sources and scopes. Neither was rerun by this
documentation task, and no additional manual success is inferred from them.

**Original automated implementation report:**
[phase2-validation.txt](phase2-validation.txt) records 26 successful
OS-Ken/topology/socket tests plus five successful Ubuntu Mininet structure/safety
tests: **31 distinct passing tests historically**. The first interpreter's
`Ran 31 ... OK (skipped=5)` is not 31 passes by itself; the other five were run
successfully with system Python. That file also records a TCP 6653 smoke test
and a failed sudo attempt, so it does not establish a successful live integration
run. The file is preserved unchanged as the original record.

**User-reported manual validation:** the request for this documentation update
supplied the following observations from October 15, 2026:

| Observation | What it supports | What it does not establish |
| --- | --- | --- |
| h2→h5 had 0% packet loss while the backbone was disabled | Host connectivity over an available alternative during that manual test | Loss-free behavior for every host pair or during all convergence intervals |
| The forward rule on s2 used s2-eth2 instead of failed s2-eth4 | An alternative egress decision at s2, using a configured ring port | An independently traced end-to-end sequence of intermediate switches |
| Priority-10 FlowRider forwarding rules processed packets | Installed rules were used for forwarding in the observed test | Complete counters for every switch, measured throughput, or latency |

Raw transcripts for those manual observations were not supplied with this request.
Initial/recovery `pingall`, the full seven-to-six-to-seven log sequence, TC output,
cleanup/restart and successful automated live execution must retain their own
evidence before being marked passed. This does not invalidate the reported
failover result; it limits the completion claim to the observations provided.

### Phase 2 verification checklist

Use a fresh copy of this checklist for a complete acceptance run. Items start
unchecked deliberately; attach commands, output/log filenames and dates before
checking them. The historical observations above remain a separate record.

- [ ] Controller starts successfully.
- [ ] Six OVS switches connect using OpenFlow 1.3.
- [ ] Seven links are recognized.
- [ ] Initial `pingall` passes.
- [ ] Forwarding rules appear in OVS and relevant counters show packet hits.
- [ ] Link failure is detected on the configured endpoint ports.
- [ ] Alternative forwarding works without the failed backbone port.
- [ ] Link restoration is detected.
- [ ] Connectivity succeeds after recovery.
- [ ] Bandwidth and delay configuration are verified at the default scale.
- [ ] Mininet cleans up successfully without removing unrelated bridges.
- [ ] Network restarts successfully with the same controller.
- [ ] Automated unit, simulated-peer and real Mininet integration tests pass.

### Completion criteria and phase boundaries

| Category | Current evidence and acceptance condition |
| --- | --- |
| Functionality implemented | Six-switch wiring, static topology state, explicit unicast paths, loop-safe broadcast, flow invalidation and basic failure/recovery are implemented. Code presence alone is not live verification. |
| Manual validation performed | The reported failover observations above are historical manual evidence. Complete Tests 1–9 and retain the initial/recovered connectivity, link-state, rule/counter, TC, cleanup and restart outputs for a full manual acceptance record. |
| Automated validation performed | The 31-test report is historical. For final acceptance, retain successful outputs for the unit/structure and socket suites, plus a real `--repeat 2` integration run with the final success marker and exit status 0. A skipped or sudo-denied live run is not a pass. |
| Known limitations | Fixed static topology and IPv4/ARP lab; no silent remote-link discovery, hitless convergence, full path tracing from a single flow dump, production HA, or throughput/latency proof from TC configuration alone. These limits must remain explicit. |

Mark Phase 2 **COMPLETE** when the manual checklist and all automated layers have
successful, retained execution evidence for the configured lab, including both
live repetitions and cleanup, with no unexplained forwarding/protocol errors.
Until that evidence is available, retain **PARTIALLY COMPLETE** and identify the
specific checks still outstanding. This documentation update does not upgrade
the verdict merely by adding commands or recording one manual failover result.

Telemetry-driven ML prediction and dynamic predictive routing belong to
**Phase 3 and Phase 4**, not Phase 2. They are not prerequisites for these checks
and must not be enabled just to make the Phase 2 verification pass.

## Primary references

- [OS-Ken CLI removal release notes](https://docs.openstack.org/releasenotes/os-ken/2026.1.html)
- [OS-Ken OpenFlow 1.3 messages](https://docs.openstack.org/os-ken/latest/ofproto_v1_3_ref.html)
- [Mininet TCIntf bandwidth/delay configuration](https://mininet.org/api/classmininet_1_1link_1_1TCIntf.html)

The installed 4.2.2 `app_manager.py`, `ofp_handler.py`, OpenFlow encoders, native
hub, and Ubuntu Mininet `link.py`/`node.py` were also inspected directly; installed
behavior takes precedence over older manager-launch examples in online docs.
