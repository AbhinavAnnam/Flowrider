# FlowRider

FlowRider is a six-switch predictive SDN research project. An OS-Ken/OpenFlow
1.3 controller compares static delay-weighted Dijkstra with routes weighted by
saved Random Forest forecasts. A read-only React/Vite/Tailwind dashboard shows
recorded measurements, controller decisions and actual forwarding evidence.

## Fresh-clone dashboard setup

Use Linux or WSL, Python **3.10+ with venv support**, and Node **22.12+** with npm.
The replay dashboard needs only NetworkX; OS-Ken, Mininet, sudo and the original
experiment/model folders are unnecessary.

```bash
git clone https://github.com/AbhinavAnnam/Flowrider.git
cd Flowrider
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-demo.txt
npm --prefix frontend ci
bash scripts/start_demo.sh replay --port 8081
```

Open **http://localhost:8081**. The launcher builds the frontend and starts one
loopback API/server. Ctrl+C stops it. It uses `FLOWRIDER_PYTHON` when explicitly
set, otherwise the repository `.venv/bin/python`, otherwise `python3` on PATH.
It never starts a controller, Mininet, traffic, privileged commands or training.
After installation, the recorded demonstration works offline.

Both portable assets are tracked in Git:

- `data/demo-dashboard/six-switch.json`: validated six-switch ABBA measurements,
  route events, original timestamps, snapshot/counter evidence and Phase 5 recovery.
- `data/demo-replay.sqlite`: the independent historical Phase 3 recording.

No data preparation is needed after cloning. Missing or invalid assets produce
unavailable/error messages; there are no substitute fixture measurements. Restore
missing tracked files from Git. To select a different prepared package, pass
`--evidence-package /path/to/package.json` to the launcher. The optional
`scripts/prepare_dashboard_data.py` exporter is for maintainers with original
raw evidence; it is not a demo prerequisite and refuses overwriting evidence.

## Faculty walkthrough

The six navigation sections are Overview, Network Topology, Predictive Routing,
Link Failure & Recovery, Performance Comparison and Event Timeline.

1. Inspect **Performance Comparison** and the four individual ABBA trials.
2. Select predictive trial **02** or **03**. Inspect the original six-switch,
   seven-link topology, S2–S5 measurements/forecasts and the existing 80% gate.
3. Click **Next verified reroute**. It cycles through actual ML-triggered traffic
   transitions, selects a matching installed-cookie snapshot, and highlights the
   old/new paths. Event Timeline preserves the reason, exact version, activation
   time and separate positive-counter evidence time. Each ML trial has two such
   changes. Static baseline and amber recommendations remain distinct from the
   green verified installed path.
4. Open **Link Failure & Recovery**. Use **Before Failure**, **Link Down /
   Rerouting**, and **Link Restored** to select real Phase 5 snapshots. Inspect
   timestamps, epochs, down ports, affected h2→h5 path, invalidation, temporary
   `[2,1,6,5]` route and restoration. The slider and event history remain available.
5. Choose **Phase 3 replay** for its original Start/Pause/Reset/Next event controls.
   Its calculated route previews do not prove installed packet paths. **Live** is
   a separate read-only view requiring an active controller database; pass
   `--live-db /path/to/live.sqlite` when starting the dashboard.

See [the short presentation guide](docs/demo.md).

## Verified six-switch results and limits

Controlled Mininet ABBA order: baseline → predictive ML → predictive ML → baseline.
Values below are **equal-weight means of two trials per mode**, derived from the
portable validated evidence for `phase6c-20261028T151727708712Z`.

| Metric | Static Dijkstra | Predictive ML |
| --- | ---: | ---: |
| Average RTT (ms) | 50.348 | 24.547 |
| P95 RTT (ms) | 82.200 | 44.850 |
| ICMP loss (%) | 4.889 | 0.000 |
| Aggregate UDP goodput (Mbps) | 127.463 | 142.431 |
| UDP loss (%) | 10.124 | 0.000 |
| Congestion exposure (link-seconds) | 232.513 | 460.029 |

**Congestion exposure increased under ML by 97.85%.** This unfavorable result
remains visible. These are descriptive results from a small controlled study,
not statistical significance, a general guarantee, or proof of advance-warning
lead time. The P95 value is a mean of per-trial P95s, not a pooled percentile.

Recorded proof is historical, never a claim that switches are currently active.
A recommendation is separate from installation/readback; verified traffic changes
require exact-cookie positive counters on every hop in the actual traffic
direction. The real Phase 5 failure/restoration window has 52 retained snapshots
and host connectivity checks. Positive per-hop counters for its temporary recovery
routes were not retained, so those routes are labeled installed/rule-readback
rather than independently verified packet forwarding.

## Architecture

- `topology_config.py`, `topology_state.py`: the original six switches, seven
  links, host/port mapping and link-failure/recovery state.
- `controller.py`, `predictive_routing.py`: loop-safe forwarding and versioned,
  bidirectional OpenFlow installation, barriers, readback and counter audits.
- `telemetry*.py`, `prediction.py`: five-second measurements, SQLite history and
  causal +15-second RF forecasts. Existing weight: `D₀ × (1 + 25 × Û⁴)`; policy
  retains the 80% gate, 10% improvement, two confirmations and 30-second cooldown.
- `phase3.py`, `research_phase3.py`: preserved offline training/evaluation code.
  No model training occurs during dashboard startup.
- `dashboard_evidence.py`, `demo_data.py`, `demo_server.py`, `routing_state.py`:
  portable evidence, Phase 3 replay and read-only live APIs (`/api/benchmark`,
  `/api/state`, `/api/routing`). POST controls change only dashboard selection.
- `frontend/src/`: existing React dashboard and charts. `main.py` and
  `traffic_engine.py` retain the independent simulation.

## Automated checks (no live Mininet)

With the minimal dashboard environment:

```bash
python -m unittest discover -s tests -p 'test_demo*.py' -v
python -m unittest discover -s tests -p test_dashboard_evidence.py -v
npm --prefix frontend run test:unit
npm --prefix frontend run build
npx --prefix frontend playwright install chromium
npm --prefix frontend test
```

Playwright starts a temporary local API on port 18080 and uses the built frontend.
Its faculty-workflow test uses the tracked evidence, including both verified ML
transitions and the three recovery controls. Browser system libraries must be
available; use your distribution's Playwright prerequisites if missing. An
existing compatible Chromium can be selected with `PLAYWRIGHT_CHROMIUM_EXECUTABLE`.

For the **full controller/research regression suite**, use a compatible Python
(tested with 3.10) and install the retained controller/scientific dependencies:

```bash
python -m pip install -r requirements.txt -r requirements-ml.txt 'scikit-learn==1.7.2'
python -m unittest discover -s tests -q
```

When Ubuntu's Mininet Python package is installed, cover the environment-specific
unit checks separately; these use mocks and do not launch a lab:

```bash
PYTHONPATH=tests /usr/bin/python3 -m unittest test_phase5_lifecycle test_mininet_topology test_phase6_smoke test_phase6c_collector -q
```

## Optional live lab

Live execution additionally needs OVS, Mininet, `tc`, `ip`, `iperf3`, `ping`,
authenticated sudo and no conflicting lab interfaces. Install OS-Ken from
`requirements-controller.txt`; use `requirements-ml.txt` and **scikit-learn 1.7.2**
for the original frozen RF artifact. That trusted model and full raw experiments
are intentionally not bundled in Git. No retraining is needed for recorded replay.

In one terminal, using the controller/scientific environment:

```bash
python -u controller.py --ofp-listen-host 127.0.0.1 --ofp-tcp-listen-port 6653 \
  --routing-mode baseline --telemetry-enabled --telemetry-db data/live.sqlite
```

In another terminal, optionally launch the original topology:

```bash
sudo /usr/bin/python3 mininet_topology.py --controller-port 6653
```

For predictive operation, use `--routing-mode predictive_ml` and
`--telemetry-model /absolute/path/to/the/original/model.pkl`. The dashboard does
not supply or start that model/controller. Stop the lab/controller separately;
no automatic global network cleanup is performed by the dashboard.

Historical methods and evidence remain in [Phase 2](docs/phase2.md),
[Phase 3](docs/phase3.md), [Phase 5](docs/phase5.md),
[Phase 6 baseline](docs/phase6-baseline.md), [Phase 6B](docs/phase6b/report.md),
and [Phase 6C](docs/phase6c/README.md). Earlier documents describe the status at
the time of those runs; the current faculty workflow and six-switch results are
summarized here. Four-switch experiments and existing tests are preserved.
