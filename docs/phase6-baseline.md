# Phase 6: static-weight Dijkstra control mode

Explicit `baseline` and `predictive_ml` modes share the Phase 5 verified
forwarding engine. No topology, Random Forest artifact, weight formula, safety
threshold or dashboard was changed. No comparative benchmark has been run.

## Routing behavior

| Launch | Selection and forwarding |
| --- | --- |
| No routing mode or predictive flag | Existing Phase 2 forwarding, unchanged |
| `--predictive-routing` | Existing Phase 5 predictive behavior, unchanged alias |
| `--routing-mode predictive_ml` | Phase 5 weighted selection: `base_delay * (1 + 25 * predicted_utilization ** 4)` |
| `--routing-mode baseline` | Static NetworkX weighted Dijkstra: `weight = base_delay` |

Explicit modes require `--telemetry-enabled`. A contradictory
`--predictive-routing --routing-mode baseline` is rejected. Omitting the new
mode never silently enables the verified engine or changes legacy forwarding.

`topology_config.py` supplies the same six switches, seven links and endpoint
ports to Mininet and `TopologyState`. Ring links have base delay 5 ms; s2-s5
has delay 2 ms. `TopologyState.active_graph()` removes unavailable links and
the shared `network_topology.calculate_shortest_path()` uses NetworkX's weighted
Dijkstra path calculation. Static weights are copied from `base_delay` and are
never taken from measured or predicted utilization. The baseline graph copy
does not mutate topology or ML weights.

The baseline starts with the shortest available path, e.g. h1/h4
`[1,2,5,4]`. It never creates a predictive reroute candidate or switches paths
because utilization rises. It still invalidates/reinstalls after connectivity
changes, verifies actual rules, audits them, repairs missing rules and refreshes
hard timeouts. A rule version/cookie may change during maintenance while its
path stays the same. Ordinary MAC/ARP handling, VLAN versioning, barriers,
exact flow-stat readback, quarantine and failure recovery remain shared.

Both modes continue five-second telemetry sampling. If the same model is
provided in baseline mode, its forecasts are collected as advisory measurements
but never consulted for static route selection. Baseline also works without a
model. For later comparisons, use the same model/inference setup in both runs
and disclose that choice rather than mixing different controller overheads.

## Exact controller and live smoke commands

Run one controller/lab at a time with fresh directories. Keep the existing model
and all Phase 5 evidence. Prepare a common description of the smoke traffic
(this creates configuration only, not measurements):

```bash
cd /home/bob_the_bob/Flowrider
mkdir -m 700 data/phase6-config-001
cat > data/phase6-config-001/smoke.json <<'JSON'
{"generator":"integration_phase6","protocol":"ICMP","stages":["initial_pingall","s2-s5_down_h1-h4","s2-s5_up_h1-h4","final_pingall"],"randomness":null}
JSON
mkdir -m 700 data/phase6-baseline-001
```

Terminal 1, baseline controller:

```bash
~/.pyenv/versions/3.10.16/bin/python -u controller.py \
  --ofp-listen-host 127.0.0.1 --ofp-tcp-listen-port 16653 \
  --routing-mode baseline --telemetry-enabled \
  --telemetry-bandwidth-scale .1 --trial-id phase6-baseline-001 \
  --telemetry-db "$PWD/data/phase6-baseline-001/telemetry.sqlite" \
  --telemetry-model "$PWD/data/research-20261015T180515Z/evaluation/model.pkl" \
  --traffic-config "$PWD/data/phase6-config-001/smoke.json" \
  > data/phase6-baseline-001/controller.log 2>&1
```

Terminal 2, baseline Mininet smoke:

```bash
sudo /usr/bin/python3 tests/integration_phase6.py \
  --routing-mode baseline --controller-port 16653 --bandwidth-scale .1 \
  --database "$PWD/data/phase6-baseline-001/telemetry.sqlite" \
  --output "$PWD/data/phase6-baseline-001/smoke" \
  > data/phase6-baseline-001/mininet.log 2>&1
```

The smoke checks fresh mode-matched state, initial/final zero-loss `pingall`,
positive exact-cookie hop counters, independent OVS dumps and host delivery.
It also tests s2-s5 failure/restoration and, for baseline, restoration of the
base-delay shortest path. A PASS is written only after scoped Mininet teardown
and post-run preflight. Existing output directories are refused. Ctrl+C or TERM
unwinds the owned network; it does not stop unrelated controllers or networks.
Stop this trial's controller with Ctrl+C after the smoke completes.

For the corresponding predictive controller, create a separate directory and
use the same model, link scale and traffic configuration:

```bash
mkdir -m 700 data/phase6-predictive-001
~/.pyenv/versions/3.10.16/bin/python -u controller.py \
  --ofp-listen-host 127.0.0.1 --ofp-tcp-listen-port 16653 \
  --routing-mode predictive_ml --telemetry-enabled \
  --telemetry-bandwidth-scale .1 --trial-id phase6-predictive-001 \
  --telemetry-db "$PWD/data/phase6-predictive-001/telemetry.sqlite" \
  --telemetry-model "$PWD/data/research-20261015T180515Z/evaluation/model.pkl" \
  --traffic-config "$PWD/data/phase6-config-001/smoke.json" \
  > data/phase6-predictive-001/controller.log 2>&1
```

In terminal 2, repeat the same smoke command with `--routing-mode predictive_ml`
and both database/output paths under `data/phase6-predictive-001`. This low-load
smoke does not require or claim an RF congestion trigger; the existing Phase 5
acceptance harness remains the test for that behavior. Read actual routes with:

```bash
~/.pyenv/versions/3.10.16/bin/python routing_state.py \
  data/phase6-baseline-001/telemetry.sqlite --events 100
```

## Trial records and reproducibility

No SQLite schema rewrite was necessary. New sessions' `config_json` includes
`routing_mode`, explicit or generated `trial_id`, `topology_version`, supplied
`traffic_configuration`, model path, bandwidth scale, sampling interval and
horizon. The SQLite session UUID still identifies the original data stream.
Routing events/snapshots add mode, trial ID and the deterministic logical-wiring
hash (`e279097186c5af01` for the current configuration), alongside existing
topology epochs, recommendation, installed version, observed counters and
measurement/verification timestamps. Snapshots expose the effective
`routing_weight` per active link. Baseline reports `automatic_rerouting=false`
for utilization-driven routing; topology recovery remains automatic.

The version hash covers logical switch/host/link/port configuration. Physical
scale is recorded separately; retain Mininet's applied-TC logs as evidence of
actual bandwidth/delay settings. The unchanged lab has 100-Mbps ring links,
200-Mbps s2-s5, 100-Mbps host access at scale .1, 5/2-ms inter-switch delays and
1000-packet inter-switch queues. Host-link defaults are also shared unchanged.

For subsequent offered-load trials, reuse `collect_phase3.py` rather than a new
traffic generator. Prepare one common seeded plan and use it in both controller
launches instead of the ICMP smoke configuration:

```bash
/usr/bin/python3 - <<'PY'
import json
from pathlib import Path
from collect_phase3 import traffic_schedule
plan = dict(generator='collect_phase3', seconds=600, seed=42, bandwidth_scale=.1,
    stages=traffic_schedule(600,42), flow_pairs=[['h1','h4'],['h3','h6']],
    measurement_interval_seconds=5)
with Path('data/phase6-config-001/traffic.json').open('x') as output:
    json.dump(plan,output,indent=2)
PY
```

Use fresh trial directories/databases for these load tests. For each controller
mode, collect with identical parameters, changing only trial/output paths:

```bash
sudo /usr/bin/python3 collect_phase3.py \
  --controller-port 16653 --bandwidth-scale .1 --seconds 600 --seed 42 \
  --database "$PWD/data/phase6-baseline-load-001/telemetry.sqlite" \
  --output "$PWD/data/phase6-baseline-load-001/traffic"
```

For predictive mode substitute `phase6-predictive-load-001` paths, after starting
that mode with the same `traffic.json` plan. The collector validates a recorded
`collect_phase3` plan against actual seed, durations, stage schedule, host pairs,
scale and interval before traffic begins. Legacy sessions without a plan keep
their old behavior. `traffic.json` output adds mode/trial/topology identifiers
to existing actual stage timestamps and flow records. The existing collector's
minimum-duration/data-quality checks are not relaxed. No such load run was
executed here. A seed makes the offered schedule repeatable, not kernel/network
timing deterministic; compare timestamps and matched conditions before making
any performance claim.

## Verification and limitations

Tests were written failing-first for static weights, ignored congestion,
verified installation, common Dijkstra behavior, topology failure/restoration,
mode resolution, metadata persistence and mismatched traffic-plan rejection.
The baseline uses the same safety code; a missing-readback regression confirms
it cannot claim installation from barriers alone.

A real OS-Ken TCP test with explicitly simulated switches launches the baseline
CLI, forwards packets both ways through encoded rules, persists valid telemetry
and mode/trial metadata, and verifies link-failure recovery. The existing RF
socket test now also exercises explicit `predictive_ml` selection and retains
its actual RF-trigger, alternative packet delivery and model-unavailable fallback
checks. These tests use temporary synthetic fixtures, never the historical model
or live measurements as writable test inputs.

Executed results for this change:

- Focused routing tests: **31/31 passed**; mode/provenance/traffic-plan tests:
  **3/3 passed**; baseline controller TCP test: **1/1 passed**.
- Complete existing suite plus new tests:
  `~/.pyenv/versions/3.10.16/bin/python -m unittest discover -s tests -v`:
  **172 discovered, 150 passed, 22 skipped**, in 76.503 seconds. Skips are the
  Ubuntu Mininet-dependent imports unavailable to pyenv Python.
- System Python: **18/18** Phase 5 lifecycle/launcher checks, **6/6** topology
  and teardown checks, **2/2** Phase 6 smoke guards passed. This includes the
  cases skipped in the pyenv run.
- `git diff --check`, Python compilation and the new controller/smoke CLI help
  checks passed. No frontend changes require a new UI build.

Machine-readable execution details are in `phase6-validation.json`.

Files changed: `controller.py` (mode/metadata flags), `predictive_routing.py`
(shared static selection), `topology_config.py` (logical wiring identity),
`telemetry_runtime.py` (session metadata), `collect_phase3.py` (plan validation
and trial labels), `tests/test_predictive_routing.py`,
`tests/test_routing_modes.py`, `tests/test_baseline_socket.py`,
`tests/test_predictive_socket.py`, `tests/test_phase6_smoke.py`,
`tests/integration_phase6.py`, this document, the validation JSON and README.

The saved Phase 5 full evidence at
`data/phase5-20261023T092150Z/acceptance.json` reports `passed_live_mininet`,
scenario `full`, backbone recovery and scoped cleanup. That is preserved
historical live evidence, not a live run of the new Phase 6 modes.

Live baseline smoke is **NOT RUN**: Mininet imports under system Python, but
`sudo -n true` reports that a password is required. Use the commands above from
an authenticated terminal. No zero-loss live baseline or comparative latency,
goodput, loss or improvement has been claimed. No frontend comparison dashboard,
retraining or historical experiment regeneration is included.
