# Phase 6C: controlled four-switch benchmark

Implementation is ready for privileged execution. No live Phase 6C trial has
been executed in this session: `sudo -n true` reports `a password is required`.
There are no measured Phase 6C performance results or improvement percentages.
See [report.md](report.md) for the current evidence and limitations.

From the repository root, run this single command in an authenticated terminal:

```bash
sudo /usr/bin/python3 scripts/run_phase6c.py --trials 4
```

The runner automatically selects the invoking user's existing
`~/.pyenv/versions/3.10.16/bin/python` for OS-Ken, scikit-learn and Matplotlib.
Ubuntu's `/usr/bin/python3` runs Mininet. An explicit `--controller-python PATH`
can select another compatible installation. The saved model requires exactly
scikit-learn 1.7.2; no installation or retraining happens automatically.
The standard OVS service and `tc`, `ip`, `iperf3`, and `ping` must be available.

The command runs a baseline/ML feasibility pair first. Only complete, validated
feasibility measurements and successful resource checks permit the full
baseline → ML → ML → baseline sequence. Both phases use identical physical
conditions and rates; feasibility shortens durations. Full trials offer 290
traffic seconds each; feasibility offers 185 seconds each, followed by a
20-second telemetry tail with the network still connected. Allow roughly
30–40 minutes including setup, probes, result exchange, and verification.
No successful ML switch or performance advantage is required for a valid trial.
A failed run retains its diagnostics and produces an incomplete report.

For just the shorter feasibility pair:

```bash
sudo /usr/bin/python3 scripts/run_phase6c.py --profile feasibility --trials 2
```

Use `--experiment data/NEW-NAME` to choose a new output directory. Existing
outputs are refused. Default directories have unique UTC timestamps. A directory
contains `model-compatibility.json`, resource checks, `feasibility/`,
`feasibility-analysis/`, `trials/`, and `trials-analysis/`. The latter contains:

- `report.md`: baseline/ML tables, scenario metrics including low-load control,
  descriptive mean/SD, regressions or improvements, forecast accuracy, and drops.
- `results.json`, `input-manifest.json`, `routing-timeline.csv`: numerical sources,
  hashes and separate recommendations, installation attempts, verified changes.
- `average_ms.png`, `p95_ms.png`, `goodput.png`, `loss_percent.png`, `udp_loss.png`,
  `congestion.png`, `utilization.png`, `routing.png`: measured comparisons.

Each raw trial records the immutable plan, model/source hashes, its own SQLite
session, controller/Mininet logs, actual applied/final TC state, independent OVS
flow dumps, periodic queue counters, raw timestamped ping and iperf3 results,
process exits and scoped cleanup evidence. Analysis does not modify raw inputs.

## Separately selected topology

```text
h1 -- s1 --(2 ms, 100 Mbps)-- s2 --(2 ms, 20 Mbps)-- s4 -- h2
       \                                            / \-- h4
        +--(5 ms, 100 Mbps)-- s3 --(5 ms, 100 Mbps)--+     
s2 -- h3
```

The foreground h1→h2 baseline is `[1,2,4]` with 4 ms one-way inter-switch delay.
The competing h3→h4 baseline is `[2,4]`. Both share the S2→S4 bottleneck. The
foreground detour `[1,3,4]` has 10 ms inter-switch delay and 100 Mbps capacity.
All four access links are 100 Mbps. H4 attaches to S4 port 4; host numbers are
independent of switch numbers. Link delays apply to each egress direction.

The bottleneck has a 100-packet netem queue at each endpoint; other inter-switch
queues contain 1,000 packets. TC verifies bandwidth, delay and queue size on
all 16 endpoints, including host namespaces. Actual queues/drops and both
traffic paths are inspected during the experiment. The alternative must show
sampled utilization below 80%; baseline sustained congestion must show both
queue/drop pressure and at least two ≥80% link samples. Absence of either
condition invalidates the experiment instead of prompting rate tuning.

The six-switch topology remains the default for ordinary controller/Mininet
launches, with its original topology identity. Four-switch selection is explicit:

```bash
/usr/bin/python3 mininet_topology.py --topology phase6c --describe
# Controller and interactive lab, when separately launched, both need:
# --topology phase6c
```

The experiment reuses switch names and therefore refuses a running historical
lab with conflicting names. It never stops an unrelated network or runs `mn -c`,
`pkill`, or `killall`. Only its own network and child processes are stopped.

## Fixed traffic and routing

Every stage uses two concurrent UDP clients in the forward direction,
with the same schedule in every routing mode. Seed 42 sets the low rate to
1 Mbps per flow (other seeds choose 1 or 2). Rates were selected from capacity
arithmetic before measurements; they are not adjusted to obtain an ML benefit.

| Regime | Full seconds | Feasibility seconds | Mbps per flow |
| --- | ---: | ---: | ---: |
| Low / warmup control | 40 | 25 | seeded 1–2 |
| Buildup | 30 | 15 | 6 |
| Buildup | 30 | 15 | 12 |
| Sustained shared bottleneck | 90 | 45 | 16 |
| Burst | 15 | 15 | 18 |
| Burst gap | 15 | 15 | seeded 1–2 |
| Burst | 15 | 15 | 18 |
| Burst gap | 15 | 15 | seeded 1–2 |
| Recovery | 40 | 25 | seeded 1–2 |

Measurement contract 2 gives each stage at least 15 seconds so the unchanged
five-second OpenFlow audits can establish counter increases even across a hard
refresh. Zero counters can establish a comparison baseline; they never prove
forwarding. The historical contract 1 remains readable. See the
[feasibility failure investigation](feasibility-failure.md).

The 32 Mbps sustained and 36 Mbps burst aggregate offers exceed the 20 Mbps
bottleneck and remain below alternative/access capacity. Actual delivered rates
and congestion still require measurement. Stage start/finish timestamps expose
connection, result-exchange and probe overhead; five-second telemetry may hide
short bursts. Feasibility still retains ≥15 s model history, the +15 s horizon,
two confirmations, and 30 s cooldown. No routing safety parameter is overridden.

Baseline weights are `base_delay`. Predictive weights remain
`base_delay * (1 + 25 * predicted_utilization ** 4)`. The existing RF, clipping,
80% forecast gate, minimum improvement, audits, versioned bidirectional rules,
barriers, exact readback, fallback and cooldown are unchanged. RF predictions
are never replaced with current measured load, and no retraining occurs.

## Definitions and interpretation

RTT probes run at 5 Hz for both pairs during each actual traffic stage, with
wall timestamps and one-second timeouts. Mean, interpolated P95 and maximum use
received replies only. Loss is `(sent-received)/sent`; all-loss runs retain
unavailable RTT instead of a fabricated zero. UDP loss uses receiver-reported
lost/expected datagrams. Receiver Mbps per flow is received payload bits divided
by summed receiver durations. Aggregate wall goodput divides payload bits by
the actual traffic envelope; stage plots use each stage's recorded envelope.

Utilization is the existing max-direction OpenFlow TX / physical link capacity,
not receiver goodput. Queueing and accounting location can make offered-side
counters exceed 100%; preserve raw values. The unchanged predictor clips output
to [0,1]. Congestion exposure sums covered ≥80% intervals across all four links
in link-seconds. It does not measure end-to-end congestion time. Queue-drop
reports use netem counters without adding duplicate parent-HTB drops.

A trial requires ≥95% telemetry coverage on every link and ≥80% stage coverage.
Positive controller readback and increasing per-hop counters plus independent
OVS dumps verify both directions on each measured pair. Timeline counts exclude
same-path refreshes and distinguish recommended from verified installed paths.
An ML route change is evidence of forwarding, not evidence of an accurate
advance prediction or improved performance.

Forecasts are matched to +15 s targets within the existing 2 s tolerance. The
existing Phase 3 valid-history evaluation is also reused for real records.
Lead times require a measured rising 80% crossing and an earlier qualifying
forecast issued below that threshold. Missing matches produce unavailable
values, never an assumed 15 s lead. In ML runs the policy changes later load,
so closed-loop forecast errors do not isolate open-loop predictive skill.

The report provides equal-trial means and sample SD, scenario-level control and
congestion measurements, and descriptive differences. Two trials per mode are
insufficient for a strong statistical conclusion. Host CPU/kernel scheduling,
ICMP treatment, traffic-transition overhead, five-second sampling, and model
capacity shift remain limitations. If ML loses or does not reroute, retain that
outcome without changing the model or conditions.

## Individual trials and offline analysis

Advanced/manual commands are retained; the single command above is preferred:

```bash
/usr/bin/python3 scripts/run_phase6c.py --experiment data/NEW-RAW --init
sudo /usr/bin/python3 scripts/run_phase6c.py --experiment data/NEW-RAW --trial 1
# Repeat --trial 2, 3 and 4 in order; prior trials must pass.
~/.pyenv/versions/3.10.16/bin/python docs/phase6c/analyze.py \
  --experiment data/NEW-RAW --output docs/phase6c/NEW-ANALYSIS
```

The legacy six-switch plan API and `--topology six --init` are retained for
existing uncommitted tests/work. Automatic Phase 6C execution always selects
this independent four-switch experiment.
