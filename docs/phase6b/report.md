# FlowRider Phase 6B: one paired 600-second experiment

**Conclusion:** real ML rerouting occurred, but these runs do not demonstrate a throughput or loss improvement. RTT/latency was not recorded. This is one pair, not a statistical result.

## Results

| Metric | Baseline | Predictive ML | ML − baseline | Change |
| --- | ---: | ---: | ---: | ---: |
| Duration-weighted mean per-flow sender throughput (Mbps) | 51.942116 | 51.942055 | -0.000061 | -0.0001% |
| Duration-weighted mean per-flow receiver goodput (Mbps) | 51.880148 | 51.874024 | -0.006124 | -0.0118% |
| Aggregate received payload / actual traffic window (Mbps) | 103.011075 | 102.897720 | -0.113355 | -0.1100% |
| Received payload (bytes) | 7,791,460,664 | 7,791,466,456 | +5,792 | +0.0001% |
| Lost UDP datagrams | 0 | 0 | +0 | N/A (zero baseline) |
| UDP loss rate | 0.000000% | 0.000000% | +0.000000 pp | N/A (zero baseline) |
| Mean link utilization | 37.866% | 34.825% | -3.041 pp | -8.0321% |
| Congestion exposure ≥80% (link-seconds) | 1,075.053 | 965.074 | -109.979 | -10.2301% |
| Covered link-time at ≥80% | 25.409% | 22.847% | -2.562 pp | -10.0814% |
| ML-recommended path changes, all pairs | 0 | 211 | +211 | N/A (zero baseline) |
| ML-recommended changes, traffic pairs | 0 | 81 | +81 | N/A (zero baseline) |
| Qualifying candidate events (not switches) | 0 | 105 | +105 | N/A (zero baseline) |
| Verified installed path changes, all pairs | 0 | 25 | +25 | N/A (zero baseline) |
| Installed path changes, traffic pairs | 0 | 8 | +8 | N/A (zero baseline) |
| Traffic-pair non-baseline activations | 0 | 4 | +4 | N/A (zero baseline) |
| Traffic-pair returns to baseline | 0 | 4 | +4 | N/A (zero baseline) |
| Traffic-pair changed versions with positive hop counters | 0 | 8 | +8 | N/A (zero baseline) |
| Failed/quarantined transitions | 0 | 0 | +0 | N/A (zero baseline) |
| Average / P95 RTT or one-way latency | Unavailable | Unavailable | N/A | N/A |

Sender throughput and receiver goodput use total payload bits divided by the sum of their respective per-flow measured durations; unequal stage durations are not averaged equally. The two concurrent flows imply approximately twice the mean per-flow rate, not a single-flow sum over 1,200 seconds. UDP payload goodput excludes protocol headers. Loss uses total receiver-reported lost datagrams / total expected datagrams.

Both runs received exactly all their reported sent bytes. ML receiver goodput is slightly lower because receiver reporting intervals are slightly longer, not because measured loss increased. The actual-window aggregate rate also includes stage startup/transition overhead; ML took about 0.667 seconds longer for that window. Payload totals differ by only four 1,448-byte datagrams. Do not interpret these small timing/rounding differences as a causal performance disadvantage or benefit. Stored UDP jitter is not latency, so no RTT mean/P95 was inferred.

Sampled ≥80% congestion exposure was 10.23% lower with ML in this pair. This is a link-level observation, not a measured latency/throughput benefit. The max-direction, unweighted mean does not measure total network byte-work or energy efficiency.

## Comparability and completion

- topology_version: MATCH / PASS.
- traffic_configuration: MATCH / PASS.
- settings: MATCH / PASS.
- model_versions: MATCH / PASS.
- snapshot_topology: MATCH / PASS.
- routing_safety_policy: MATCH / PASS.
- no_runtime_topology_change: MATCH / PASS.
- flow_stage_identity: MATCH / PASS.
- iperf_version_system: MATCH / PASS.
- baseline: 29 stages, 58 complete UDP JSON results; traffic start 2026-10-25T20:57:41.098746+05:30; valid traffic-window link observations 847; telemetry coverage 99.891% of seven-link window; traffic envelope 605.096932s; collection elapsed including tail 626.621431s. Traffic-window prediction statuses: {'insufficient_history': 21, 'valid': 826}; first valid forecast +15.661s relative to traffic start.
- predictive_ml: 29 stages, 58 complete UDP JSON results; traffic start 2026-10-25T21:10:47.510357+05:30; valid traffic-window link observations 847; telemetry coverage 99.616% of seven-link window; traffic envelope 605.763974s; collection elapsed including tail 627.421793s. Traffic-window prediction statuses: {'insufficient_history': 21, 'valid': 826}; first valid forecast +17.329s relative to traffic start.
- Maximum matched-stage wall-duration difference: 0.057812s. Maximum cumulative aligned stage-start difference: 0.672784s. See `stages.csv`.
- The recorded plans are identical: seed 42, 600 planned seconds, scale 0.1, topology e279097186c5af01, same recorded model path/version, poll interval and policy. Every stage uses **h4→h1 and h6→h3**, because all saved reverse flags are true; these are the specified host pairs in reverse direction.
- The one topology epoch during each traffic window is stable. Initialization/teardown topology changes outside the window are excluded from congestion/transition counts.
- All expected iperf JSON results contain completed sender/receiver summaries without an iperf error. Client exit codes and collector exit status were not saved, and `traffic.json` is also written in a finally block; complete data is not a formal successful process-exit certificate.
- **Both controller logs contain post-traffic KeyboardInterrupt / HubThread.kill AttributeError shutdown tracebacks.** Their preceding timestamped topology events occur after traffic. No failed/quarantined route transitions or controller critical log lines were found during the measurement window. Clean controller shutdown cannot be claimed; the shutdown error is reported, not repaired in this analysis.
- The matching snapshots/configuration document logical topology, active capacities, delays and policy. Independent applied qdisc/queue logs, run exit status and final ping summaries are not saved here; physical queue equivalence and global cleanup/connectivity completion cannot be independently certified from these directories.
- Trials were sequential (baseline first), with different controller warmup times, telemetry phase alignment, real stage overhead and kernel/CPU scheduling. Frozen-model identity matches by recorded version/path; no per-trial model-byte digest was captured.

## What routing evidence proves

Recommendation changes count consecutive path changes in snapshots (first observation excluded), not every poll or recommendation timestamp. Candidate events are qualifying confirmation updates, not successful switches. Installation counts use route_activated with a different previous_path and exact OpenFlow-stat verification, excluding initial installation and same-path hard refresh.

ML had 25 installed path changes across all pairs (15 to non-baseline paths, 10 returns), including 8 changes on the actual UDP pairs (4 non-baseline activations, 4 returns). Their changed versions have 8 saved positive-counter proofs on every hop in the **actual sending direction**. This distinguishes real forwarding from inactive-pair rule installation. Raw independent ovs-ofctl dumps and packet captures were not retained for these benchmark runs; verification here is the controller's persisted exact-flow readback.
At 25/25 ML activations, the previous path had a fresh recorded forecast at or above 80%. Measured utilization on that path was also at least 80% for 15/25 activations. These predicates are distinct: predictive routing acts on the forecast, not a requirement that current measurements already exceed threshold.
The minimum interval between two switches for the same pair was 40.005s; no failed transitions/quarantine were recorded. Recommendations varied more frequently than installations, demonstrating confirmation/hysteresis filtering. Multiple returns to previous paths are visible; one trial does not establish general routing stability or bound sub-poll interruption/loss. See `route-changes.csv` and `installed-routes.png`.

## Utilization methodology

Metrics are restricted to each run’s first-stage start through last-stage finish, excluding controller startup and the 20-second collection tail/teardown. Link utilization is max-direction TX / configured capacity, including link-layer traffic overhead; it is not UDP goodput. Valid observations summarize the preceding interval. The script uses adjacent monotonic sample deltas, clips intervals at window edges, rejects invalid samples/gaps over 7.5 seconds and reports covered time. Congestion exposure sums covered seconds at utilization ≥0.8 across links; overlapping links contribute separately (link-seconds, not end-to-end congestion duration). Mean utilization is time-weighted across seven links, without capacity weighting. Sample-window averaging can hide brief bursts.

## Graphs and reproducibility

- `goodput.png` / `.svg`: matched-stage aggregate receiver goodput and ML-minus-baseline differences.
- `utilization.png` / `.svg`: recorded per-link samples aligned to each trial’s actual first-stage start.
- `congestion-exposure.png` / `.svg`: per-link ≥80% exposure over the measured traffic windows.
- `installed-routes.png` / `.svg`: actual installed path records for both traffic pairs; canonical path orientation is shown, while UDP sends in reverse.

```bash
~/.pyenv/versions/3.10.16/bin/python docs/phase6b/analyze.py \
  --baseline data/phase6-benchmark-baseline-001 \
  --predictive data/phase6-benchmark-predictive-001 --output docs/phase6b
/usr/bin/python3 -m unittest discover -s docs/phase6b -p test_analysis.py
```

The script opens SQLite in read-only mode, verifies input hashes before/after, and writes only generated documentation artifacts. `results.json` contains definitions, configuration, checks, per-stage metrics, route changes and log classifications. `stages.csv`, `route-changes.csv` and `utilization.csv` expose the numerical graph sources. `input-manifest.json` fingerprints every input file. Existing controller/model/topology code and historical data were not changed. Five focused arithmetic/interpretation unit tests pass, covering duration weighting, packet weighting, missing data, interval clipping/gaps and exclusion of refreshes from route switches.

## Next experiment

Repeat matched trials with counterbalanced baseline/ML order and independent repetitions before statistical inference. Add timestamped continuous RTT probes and explicit collector/controller exit/cleanup status, retaining TC/qdisc settings. For a more discriminating load, after warmup try three matched pairs h1→h4 at 60 Mbps, h2→h5 at 80 Mbps and h3→h6 at 80 Mbps: 220 Mbps offered to the shared 200-Mbps backbone while individual host accesses remain below 100 Mbps, with unused alternative directions. Verify actual bottleneck/receiver measurements and spare-path capacity; do not assume the RF will trigger or that rerouting helps. Run exactly the same scenario in both modes. No such new experiment was run here.
