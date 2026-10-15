
# FlowRider: AI-Driven SDN Traffic Control Engine

FlowRider explores predictive congestion avoidance with a Random Forest model
and delay-weighted Dijkstra routing. The simulation remains independent of the
OS-Ken/OpenFlow 1.3 lab.

## Phase 2 SDN lab

The lab has six OVS switches, seven inter-switch links, and six IPv4 hosts.
The OS-Ken controller implements static topology awareness, explicit unicast
paths, loop-safe broadcast, and route invalidation on switch/port changes.
No live telemetry or ML is integrated into the controller.

**Status: PARTIALLY COMPLETE.** Unit tests and real controller/TCP tests with
simulated OpenFlow peers pass. Live Mininet ping, failure/recovery, traffic-control
settings, and cleanup still need root execution.

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
