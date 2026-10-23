# Phase 5: automatic predictive forwarding

Status: **IMPLEMENTED; LIVE MININET ACCEPTANCE PENDING**. The user requested
that privileged live validation remain pending. Automated tests exercise the
real OS-Ken process, OF1.3 codecs, RF inference, rule readback and a simulated
packet dataplane. Those are not real Mininet/OVS measurements.

## Running the controller

Use the existing Python and saved live model. No packages, virtual environment,
controller replacement, model retraining, or frontend redesign are required.
From `~/Flowrider`, terminal 1:

```bash
mkdir -p data/phase5-manual
~/.pyenv/versions/3.10.16/bin/python -u controller.py \
  --ofp-listen-host 127.0.0.1 --ofp-tcp-listen-port 16653 \
  --telemetry-enabled --telemetry-db "$PWD/data/phase5-manual/telemetry.sqlite" \
  --telemetry-model "$PWD/data/research-20261015T180515Z/evaluation/model.pkl" \
  --predictive-routing
```

Terminal 2, with no other FlowRider lab running:

```bash
sudo /usr/bin/python3 mininet_topology.py --controller-port 16653
```

Initial connectivity and finite controlled lab traffic:

```text
mininet> pingall
mininet> h4 iperf3 -s -p 5201 > /tmp/flowrider-h4-server.log 2>&1 &
mininet> h6 iperf3 -s -p 5201 > /tmp/flowrider-h6-server.log 2>&1 &
mininet> h1 iperf3 -c 10.0.0.4 -u -b 7M -t 40 &
mininet> h3 iperf3 -c 10.0.0.6 -u -b 7M -t 40
mininet> h1 iperf3 -c 10.0.0.4 -u -b 45M -t 20 &
mininet> h3 iperf3 -c 10.0.0.6 -u -b 45M -t 20
mininet> h1 iperf3 -c 10.0.0.4 -u -b 80M -t 30 &
mininet> h3 iperf3 -c 10.0.0.6 -u -b 80M -t 30
mininet> h1 iperf3 -c 10.0.0.4 -u -b 95M -t 40 &
mininet> h3 iperf3 -c 10.0.0.6 -u -b 95M -t 40
mininet> h1 ping -c 5 h4
```

Traffic generation does not guarantee a model warning or an alternative route.
Do not inject predictions or reduce evidence requirements to manufacture a pass.
The repeatable acceptance script below supplies a longer varied schedule.

Terminal 3, inspect controller snapshots/events:

```bash
~/.pyenv/versions/3.10.16/bin/python routing_state.py \
  data/phase5-manual/telemetry.sqlite --events 30
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
sudo ovs-ofctl -O OpenFlow13 dump-flows s6
sudo ovs-ofctl -O OpenFlow13 dump-flows s5
sudo ovs-ofctl -O OpenFlow13 dump-flows s4
```

Actual path selection can differ from the example switches above. Inspect the
reported `installed_route.path`, its cookie and each hop's expected output port.
Exit the owned Mininet CLI with `exit` to remove its hosts, processes and switches;
then Ctrl+C the controller. Never run global `mn -c` to clean unrelated networks.
Port 16653 isolates this controller from any existing controller on 6653.

To compare with Phase 2 forwarding, omit `--predictive-routing`; no flag is
changed automatically by the dashboard. Existing launch scripts remain baseline.
The predictive flag requires `--telemetry-enabled`. Without a usable model,
opt-in forwarding still installs and verifies baseline paths.

## Pipeline and trigger

1. The existing native telemetry worker records real counters and predictions.
2. Only after committing a sample does it publish an `EventRoutingTelemetry`.
   All policy and OpenFlow operations run on the serialized OS-Ken application
   queue. The worker never writes controller route state or issues FlowMods.
3. The route manager uses the existing `TopologyState`, host attachments,
   configured port mappings and `calculate_shortest_path` implementation.
4. Every active link must have a fresh valid prediction from the same model and
   session, matched to its measured observation and the current topology epoch.
5. On the current active graph, each edge receives
   `ml_weight = base_delay * (1 + 25 * predicted_utilization**4)`.
   The formula, five-second polling and +15-second horizon are unchanged.
6. For each tracked communicating MAC pair, compare the recommended path with
   the installed path. At least one installed-path link must predict >=80%
   utilization. The relative gain is `(old_cost - new_cost) / old_cost`.
7. A sufficiently better candidate must persist across two distinct telemetry
   batches, with the cooldown expired. No dashboard action is involved.

Initial host communication installs the baseline in both directions. The pair
is tracked even if no model is ready. All TCP/UDP flows sharing that MAC pair
share its route; different host pairs have separate rules and cookies.

| Setting | Default | Purpose |
|---|---:|---|
| `--routing-min-improvement` | 0.10 | Require at least 10% lower predicted cost |
| `--routing-cooldown` | 30 seconds | Limit repeated route changes |
| `--routing-confirmations` | 2 | Reject a single transient candidate |
| `--routing-prediction-max-age` | 10 seconds | Reject missing/stale information |
| `--routing-operation-timeout` | 3 seconds per stage | Bound barrier/statistics waits |
| `--routing-drain-seconds` | 1 second | Retain obsolete transit entries briefly |
| Flow audit interval | 5 seconds | Recheck the exact installed entries |
| Maximum tracked host pairs | 64 | Bound controller state |
| Priority / idle / hard timeout | 20 / 30 s / 120 s | Keep existing expiration intervals |

These are initial engineering defaults, not optimized research parameters.
Baseline fallback bypasses ML cooldown when predictions fail or become stale.
A one-second controller tick detects staleness even if the worker stops sending.

## Installation, verification and failure recovery

In opt-in mode, each route version owns an internal VLAN ID and an exact cookie
in the existing `0x4652` cookie namespace. Host ingress matches untagged traffic,
pushes the version tag and sends to the first hop. Transit/egress entries match
`in_port`, source MAC, destination MAC and the version tag. Egress removes the
tag before host delivery. Host-originated VLAN-tagged packets are rejected.
This is for the existing untagged IPv4/ARP lab, not a tenant VLAN network.

Installation stages are:

1. Install new transit/egress rules for **both directions**.
2. Wait for barriers and query exact-cookie flow statistics on each switch.
3. Verify every expected table, priority, match, action and timeout. Compare
   canonical wire encodings, including SetField/output actions.
4. Replace both host ingress rules; barrier and read back the complete version.
5. Only after successful readback mark the route installed and flow-verified.
6. Retain the previous version's transit rules for the drain interval, then
   delete only that exact cookie and verify its absence before reusing its tag.

Ingress cutovers on separate switches are not simultaneous. Old and new tags
keep packets associated with one version during transition; no atomic
network-wide or zero-loss guarantee is claimed. The drain interval is a
configurable bound, not proof that every old packet has left every queue.
The installed-route field is cleared during cutover because the old full path
is no longer confirmed. The recommendation remains separately available.

On partial installation/readback failure, temporary priority-40 untagged drop
guards isolate only the affected MAC pair. The controller verifies the guards,
deletes/read-verifies that pair's route versions, installs a valid baseline,
and removes/read-verifies the guards. Guards have no expiration so an uncertain
half-installed path cannot resume accidentally; they are explicitly removed.
This recovery may briefly drop affected traffic. Unrelated pairs keep their
rules. If cleanup cannot be confirmed, the pair is quarantined and retried
after five seconds instead of being falsely marked active.

Switch/link/host attachment changes reuse Phase 2's global owned-cookie
invalidation and all-switch barriers; those existing topology-failure semantics
can briefly affect all paths. Epochs reject queued pre-change telemetry. Paths
are rebuilt from current attachments when host traffic resumes. Missing/expired
entries trigger recovery; idle pairs can deactivate, and the next packet
reinstalls their baseline. Active versions refresh at 100 seconds before their
120-second hard expiration. Audits do not reinstall unchanged healthy routes.

## State and future frontend interface

`RoutingManager` is the only authoritative routing state. SQLite stores its
history, not a second route calculation. The existing single writer adds two
session-scoped tables, preserving the Phase 3 tables and schema:

```text
routing_events:    id, session_id, timestamp, record_json
routing_snapshots: id, session_id, timestamp, record_json
```

Events include topology epoch, sequence, UTC timestamp, provenance, route ID,
and applicable paths/cookie/version/reason. They cover candidates, installation,
activation, failed installation, fallback, quarantine, expiration and cleanup.
Snapshots include connected DPIDs/ports, active links, scaled 100/200 Mbps
capacities, hosts, measured records, forecast records and each tracked pair:

- `baseline_path`: current configured-delay path.
- `recommended_route`: ML calculation or explicitly labelled baseline fallback.
- `installed_route`: version successfully programmed and read back, or null.
- `observed_route`: path verified from exact OpenFlow entries, verification time
  and per-hop packet counters. `end_to_end_delivery_verified` remains false:
  entry readback is not an independent proof of host delivery.
- `transaction_phase`, guards and route status describe incomplete operations.

Read snapshots using `routing_state.py`. When running the existing API against
the Phase 5 database, a new read-only endpoint is available:

```bash
bash scripts/start_demo.sh live --live-db "$PWD/data/phase5-manual/telemetry.sqlite"
curl http://127.0.0.1:8080/api/routing
```

The response contains `status`, `stale`, `snapshot_age_seconds`, session/source,
`snapshot`, recent `events`, and the selected dashboard `view_mode`. It always
reads the configured live database; it never substitutes the replay route.
Missing/old databases are explicitly unavailable/stale; reads create no files.
A persisted snapshot older than five seconds does not establish a current path.

The React dashboard is unchanged. Its existing `/api/routes` remains a preview;
its base-delay labels describe the old comparison view, not a Phase 5 applied
path. Use `/api/routing` or the CLI for actual controller route state. Wiring
these fields into live topology/traffic visualization belongs to the next phase.
Replay remains based on the original Phase 3 recording.

## Tests and live acceptance

```bash
~/.pyenv/versions/3.10.16/bin/python -m unittest discover -s tests -v
/usr/bin/python3 -m unittest discover -s tests -p test_mininet_topology.py -v
```

The new focused tests cover baseline/no-change, bidirectional output ports,
version tags, unrelated pairs, absent/stale/mixed-model forecasts, confirmation,
cooldown, minimum gain, epoch invalidation, partial failures, quarantine/retry,
incorrect/missing actions, stale replies, timeouts, expiration/refresh, snapshot
semantics, worker callbacks, persistence and the read-only API. Existing Phase
1–3 and replay tests are included in the full regression run.

The new socket test starts a real OS-Ken process on an ephemeral loopback port.
Simulated switches decode FlowMods, return encoded flow statistics, and execute
push/set/pop/output actions through a simulated six-switch packet fabric. A
separate tiny **synthetic RF fixture** drives deterministic congestion. It checks
actual protocol messages, automatic path change, forward/reverse packet delivery,
unrelated traffic and model-unavailable fallback. This does not evaluate the
real model's accuracy or demonstrate OVS/kernel behavior.

When ready for the pending privileged check, run in an Ubuntu terminal:

```bash
cd ~/Flowrider
bash scripts/run_phase5_validation.sh
```

The script authenticates sudo normally, starts only its own controller on 16653,
uses the existing saved live model, and creates `data/phase5-<UTC timestamp>/`.
It runs about five minutes of varied, concurrent iperf3 traffic, captures
continuous ping loss/duplicates, requires a real ML-triggered route change,
checks exact OVS entries and positive packet hits along the new path, verifies
host delivery, then checks backbone failure/recovery and final `pingall`.
It refuses existing lab names and stops only its own network/processes.
No model retraining or forecast injection occurs. The evidence directory
contains controller/Mininet logs, traffic JSON, ping summaries, applied-route
proof, snapshots and `acceptance.json` with an explicit pass/fail status.
Missing triggers, failed traffic, excessive observed loss or bad rule readback
cause failure rather than a claimed pass. End-to-end traffic observations do
not constitute an exhaustive proof that no transient loop/loss can ever occur.

Each client has a monotonic deadline measured from its own launch: configured
traffic duration plus the existing ten seconds for connection/results overhead.
TCP connection setup is explicitly limited to five seconds (`--connect-timeout
5000`); five seconds remain for normal completion. The harness does not add a
fresh duration or grace period for each sequential wait. On timeout it records
the client PID, command, elapsed time, `/proc` state, source/destination socket
state and controller snapshot before interrupting the client. Diagnostic socket
commands have two-second waits. Cleanup sends SIGINT (to flush JSON and ping
summaries), then SIGTERM and SIGKILL as needed, with three-second waits at each
step. Every child is handled independently and reaped; a failed cleanup does not
skip the remaining children.

`traffic-stages.json` is saved even when a stage fails or is interrupted. It
contains both client outcomes, exit codes, parsed final measurements, raw JSON
and separate stderr file paths, and timeout/cleanup diagnostics. Missing,
malformed, errored or shortened measurements fail the stage even if iperf exits
zero. Raw files are retained. `cleanup.json` records server/ping cleanup; client
cleanup is recorded with each stage. A successful `acceptance.json` is written
only after Mininet stops and the post-run preflight confirms that lab interfaces
and bridges are gone. The launcher forwards interruption to the privileged
runner, waits for its teardown, saves `final-routing-state.json` on failure too,
and stops/reaps its own controller and log copier. No global `mn -c`/`pkill` is
used.

The first Phase 5 live attempt, in `data/phase5-20261023T075106Z/`, **failed**
during Stage 0 after initial `pingall` passed (30/30). h1→h4 completed 40 seconds
at approximately 7 Mbps. h3→h6 sent for 40 seconds but remained active until
cleanup at about 50 seconds, without a receiver-results summary. Its server
stopped receiving around 30.35 seconds; the controller simultaneously recorded
`flow_expired` and `flow_readback_mismatch`/quarantine for that pair. This points
to a forwarding interruption and a stalled final control/results exchange,
rather than a ten-second allowance for a forty-second test. The old unhandled
`TimeoutExpired` then prevented the stage report from being saved. The iperf
mutex errors were emitted during interruption; the saved evidence does not
establish them as the original cause. The lifecycle fix leaves model/routing
behavior unchanged and will report that forwarding failure if it recurs.

Focused regression checks (no sudo or live lab):

```bash
/usr/bin/python3 -m unittest discover -s tests -p 'test_phase5_*.py' -v
/usr/bin/python3 -m unittest discover -s tests -p test_mininet_topology.py -v
bash -n scripts/run_phase5_validation.sh
```

Post-fix automated verification: **151 discovered, 133 passed, 18 skipped**
under pyenv Python 3.10.16 (62.124 seconds). The skipped tests require Ubuntu's
Mininet installation; system Python passed all **15 Phase 5 lifecycle/launcher
tests** and **6 topology/teardown tests**. A separate unprivileged runtime check
ran two real concurrent iperf3 3.16 UDP loopback flows at 7 Mbps for one second,
validated their sender/receiver JSON through `run_stage`, and reaped both
clients and servers. Python compilation, launcher shell syntax and
`git diff --check` passed. These checks do not claim live Mininet acceptance.

Prior automated verification is recorded in `phase5-validation.json` and `.txt`.
Live Mininet acceptance still requires a successful rerun; prior Phase 3 live
results cannot substitute for this Phase 5 check.

## Changed files

| File | Responsibility |
|---|---|
| `predictive_routing.py` | Policy, route versions, OF transactions/readback, recovery, snapshots |
| `controller.py` | Opt-in configuration, event handlers, packet queuing/reinjection |
| `telemetry_runtime.py` | Publish committed sample batches with topology epoch; persist routing work |
| `telemetry_store.py` | Additive SQLite routing history tables |
| `routing_state.py` | Read-only latest-session snapshot/event reader and CLI |
| `demo_server.py` | Read-only `/api/routing`; no React behavior changes |
| `scripts/run_phase5_validation.sh` | Owned-controller/live-lab acceptance launcher |
| `tests/integration_phase5.py` | Real Mininet acceptance checks, bounded traffic lifecycle and failure evidence |
| `tests/test_phase5_lifecycle.py`, `tests/test_phase5_launcher.py` | Timeout, partial-result, cleanup and launcher regressions |
| `tests/test_predictive_routing.py` | Deterministic policy/OF-state failure tests |
| `tests/test_predictive_socket.py` | Real TCP controller plus simulated switch/dataplane test |
| `tests/test_routing_integration.py` | Controller, worker, storage and API seam tests |
| `tests/test_openflow_socket.py` | Add optional transport-fixture hooks; preserve existing assertions |
| `README.md`, `docs/phase5.md` | Startup, behavior, limits and next-phase interface |
| `docs/phase5-validation.json`, `.txt` | Executed verification evidence |

## Limitations and next phase

Live OVS/Mininet verification is pending. The saved RF model was trained under
baseline routing; closed-loop redistribution changes its input distribution.
No latency/throughput benefit or prediction improvement is claimed. No tuning,
large benchmarks or reactive/predictive comparison was performed. Internal VLAN
versioning reserves IDs 1–4094 and requires the existing untagged lab. The controller
handles bounded MAC pairs, not per-application QoS or arbitrary tenant VLANs.

Future frontend work should consume the controller snapshots/events, display
measurement/prediction/installation/verification times separately, respect
staleness and in-progress phases, and keep replay visibly separate from live
state. Independent traffic-path observation could enrich the flow-entry evidence.

References used for protocol semantics: [OS-Ken OF1.3 messages](https://static.openstack.org/docs/os-ken/victoria/ofproto_v1_3_ref.html)
and [OVS action semantics](https://www.openvswitch.org/support/dist-docs/ovs-actions.7.html).
Installed OS-Ken 4.2.2 constructors/codecs and real TCP tests were also inspected.
