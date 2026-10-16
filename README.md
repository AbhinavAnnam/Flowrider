
# FlowRider: AI-Driven SDN Traffic Control Engine

FlowRider explores predictive congestion avoidance with a Random Forest model
and delay-weighted Dijkstra routing. The simulation remains independent of the
OS-Ken/OpenFlow 1.3 lab.

## Faculty dashboard demo

```bash
cd ~/Flowrider
bash scripts/start_demo.sh replay
```

Open **http://localhost:8080**. Click **Reset → Next event** to show a saved
congestion warning and a calculated alternative route. Start/pause playback to
explore the real Phase 3 recording. No Mininet or sudo is needed for replay.
The amber route is **proposed only**; this dashboard does not install OpenFlow
rules. Ctrl+C stops only the dashboard/API.

See [the faculty review guide](docs/demo_review.md) for exact live commands,
the three-minute script, source-file explanations, architecture and review
questions. [Quick demo walkthrough](docs/demo.md).

## Phase 2 SDN lab

The lab has six OVS switches, seven inter-switch links, and six IPv4 hosts.
The OS-Ken controller implements static topology awareness, explicit unicast
paths, loop-safe broadcast, and route invalidation on switch/port changes.
The Phase 2 startup command below leaves optional Phase 3 telemetry disabled.

The project owner reports successful manual Phase 2 connection, connectivity,
forwarding, failure/recovery, shaping and restart checks. Original automated
evidence and the distinction between manual and automated live verification are
preserved in the Phase 2 documentation.

From `~/Flowrider`, start the controller in one terminal:

```bash
~/.pyenv/versions/3.10.16/bin/python -u controller.py \
  --ofp-listen-host 127.0.0.1 --ofp-tcp-listen-port 6653
```

Start the new topology in another terminal:

```bash
sudo /usr/bin/python3 mininet_topology.py
```

At the `mininet>` prompt:

```text
pingall
h1 ping -c 3 h4
link s2 s5 down
h2 ping -c 3 h5
link s2 s5 up
h2 ping -c 3 h5
exit
```

Stop any previous lab session first. The runner refuses existing `s1`–`s6`
bridge/interface names and never calls global `mn -c`.

Tests without root:

```bash
~/.pyenv/versions/3.10.16/bin/python -m unittest discover -s tests -v
/usr/bin/python3 -m unittest discover -s tests -p test_mininet_topology.py -v
```

Opt-in live test, with the controller running and no interactive lab running:

```bash
sudo /usr/bin/python3 tests/integration_phase2.py --repeat 2
```

See [Phase 2 architecture, setup, mapping, troubleshooting and limitations](docs/phase2.md)
and the [captured validation results](docs/phase2-validation.txt).

## Dependencies and simulation

Use the existing Python 3.10 installation for OS-Ken. Ubuntu's Mininet uses
`/usr/bin/python3`; it does not need OS-Ken or NetworkX installed in that Python.
If missing, install only the controller's dependencies:

```bash
~/.pyenv/versions/3.10.16/bin/python -m pip install -r requirements-controller.txt
```

To run the independent simulation, install its existing dependencies and run:

```bash
~/.pyenv/versions/3.10.16/bin/python -m pip install -r requirements.txt
~/.pyenv/versions/3.10.16/bin/python main.py
```

No Ryu or Ubuntu `python3-os-ken` package is required. OS-Ken 4.2.2 has no
manager CLI; `controller.py` starts it through `AppManager.run_apps()`.

## Phase 3 telemetry and prediction

Phase 3 adds opt-in five-second OpenFlow polling, directional utilization,
session-separated SQLite history, timestamp-aligned +15 second Random Forest
forecasts, persistence-baseline evaluation, and structured prediction inspection.
Predictions never change forwarding rules or routing weights.

```bash
~/.pyenv/versions/3.10.16/bin/python -m pip install -r requirements-ml.txt
~/.pyenv/versions/3.10.16/bin/python -u controller.py \
  --ofp-listen-host 127.0.0.1 --ofp-tcp-listen-port 6653 \
  --telemetry-enabled --telemetry-db data/telemetry.sqlite \
  --telemetry-model models/live-rf.pkl
```

Start the existing topology in a second terminal. Without a trained live model,
measurements are recorded and predictions report `model_not_ready`. After varied
traffic collection, train offline; the running worker loads the artifact when
it appears or changes:

```bash
~/.pyenv/versions/3.10.16/bin/python phase3.py train data/telemetry.sqlite --model models/live-rf.pkl
~/.pyenv/versions/3.10.16/bin/python phase3.py inspect data/telemetry.sqlite
```

**Phase 3: COMPLETE WITH RESEARCH LIMITATIONS.** Four real Mininet sessions
produced 3,516 valid measurements and 850 matched live forecasts. RF reduced
live MAE/RMSE by 11.0%/13.2% versus persistence; congestion alert performance
varied across independent sessions. Phase 2 forwarding/fault recovery passed,
along with 89 distinct automated tests. Proactive routing benefits remain unproven.
See [Phase 3 methodology, schema, traffic scenarios and live checks](docs/phase3.md).
Actual results are retained in [the finalization report](docs/phase3-finalization.json),
with presentation plots in [docs/phase3-results](docs/phase3-results).

For a new independent experiment, use `bash scripts/run_phase3_research.sh` in an
authenticated Ubuntu terminal. The existing verified run is
`data/research-20261015T180515Z`; inspect its saved artifacts rather than rerunning it.
