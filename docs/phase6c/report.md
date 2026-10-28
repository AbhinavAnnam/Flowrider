# Phase 6C implementation and current evidence

Status: implementation complete; privileged performance experiment pending.
The feasibility command stopped at `sudo: a password is required` before network
creation. No live Phase 6C performance results, improvements, or verified
saved-model reroutes are claimed.

The independent four-switch experiment, reproducible traffic schedule,
feasibility-first ABBA runner, timestamped RTT/UDP collection, applied Linux TC
verification, positive bidirectional OpenFlow evidence, queue/drop collection,
forecast matching, and automatic comparison tables/graphs are implemented.
The existing six-switch default and saved model remain unchanged. Details and
commands are in [README.md](README.md); test evidence is in
[validation.md](validation.md).

| Actual Phase 6C performance measure | Baseline | Predictive ML |
| --- | --- | --- |
| Mean / P95 / maximum RTT | Not measured | Not measured |
| Congestion-stage ICMP / UDP loss | Not measured | Not measured |
| UDP receiver goodput | Not measured | Not measured |
| Utilization / netem drops / congestion exposure | Not measured | Not measured |
| Target-matched forecast error / observed advance warning | Not measured | Not measured |
| Verified installed route changes on live traffic | Not measured | Not measured |

## Frozen-model compatibility finding

The existing `rf-b6f2c2909490` artifact loads successfully with scikit-learn
1.7.2 and accepts features built by the unchanged `prediction.feature_for`
and `Predictor`. Its ten features contain utilization history, rolling mean,
slope, variability, absolute TX/RX Mbps, and capacity Mbps; switch IDs and
switch count are not inputs. The +15 s prediction horizon is unchanged.

The training-session feature records contain capacities **100–200 Mbps**.
The experiment's **20 Mbps** bottleneck is outside that range. Telemetry uses
20 Mbps as its physical utilization denominator (logical 200 × lab scale 0.1),
so there is no hidden normalization mismatch. Absolute Mbps/capacity inputs
nevertheless create a distribution shift; a finite prediction does not prove
transfer accuracy or useful congestion alerts.

The following are **synthetic sensitivity probes of the real saved model**,
using causal four-sample histories at five-second intervals, TX equal to
utilization × capacity, and zero reverse RX. They are neither measurements of
the experimental network nor performance results:

| 20 Mbps input history, oldest → newest | RF forecast | ≥80% congestion gate |
| --- | ---: | --- |
| 0.02, 0.02, 0.02, 0.02 | 0.010051 | No |
| 0.10, 0.10, 0.10, 0.10 | 0.729657 | No |
| 0.10, 0.30, 0.60, 0.95 | 0.575403 | No |
| 1.00, 1.00, 1.00, 1.00 | 0.602972 | No |
| 1.00, 0.60, 0.30, 0.10 | 0.275285 | No |

The model is schema-compatible; useful generalization to this topology remains
unverified. In particular, saturated synthetic input does not cross the existing
80% forecast gate. A 100 Mbps low-utilization sensitivity probe also forecasts
0.877595, illustrating that false alarms are possible. Do not assume monotonic
predictions, guaranteed rerouting, or an advantage over static Dijkstra.
Complete raw diagnostics, feature ranges, metadata and SHA256 are in
[model-compatibility.json](model-compatibility.json).

The benchmark does not retrain, change the forecast threshold, substitute
measured utilization, shorten cooldown, or weaken confirmations. A completed
trial with valid RF inference and **zero reroutes** is a legitimate outcome.
OpenFlow policy tests separately exercise a qualifying synthetic forecast and
verify the resulting rules and packets; they do not establish live saved-model
rerouting. Historical Phase 5/6B findings are not Phase 6C results.

## Execute the complete experiment

From `/home/bob_the_bob/Flowrider`:

```bash
sudo /usr/bin/python3 scripts/run_phase6c.py --trials 4
```

After the feasibility pair validates and resources remain sufficient, the runner
executes baseline → predictive ML → predictive ML → baseline. New timestamped
records go under `data/phase6c-<timestamp>/`. The generated report, graph PNGs,
CSV timeline and numerical JSON go into its `trials-analysis/` directory;
feasibility has its own `feasibility-analysis/`. Failed or missing trials produce
an incomplete report and no comparative conclusion. Fixture graphs are generated
only in temporary test directories and are not published as measured results.

Remaining blocker: interactive sudo authentication is required for Mininet/OVS
execution. Actual live TC settings, congestion, forwarding changes, and network
performance must therefore be established by the documented command. Even after
execution, two trials per mode provide descriptive variability, not statistical
proof of benefit. Report regressions or no difference as recorded.
