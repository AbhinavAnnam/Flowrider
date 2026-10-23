# Why the ten-stage run did not satisfy live acceptance

Source: `data/phase5-20261023T081916Z`, opened read-only. Detailed link ranges,
weights, route snapshots and candidate/activation events are extracted in
`phase5-rerouting-analysis.json`. Production code was inspected only after
examining the database, predictions, events and acceptance result.

The controller did reroute four pairs after genuine RF predictions and exact
OpenFlow verification. None was one of the two UDP pairs the acceptance harness
requires: h1/h4 and h3/h6. For those two pairs, near-symmetric loading makes two
different alternatives trade places between prediction frames. Their logged
candidate confirmation counts never exceed one. Same-path rule refreshes then
incorrectly restart cooldown, excluding later opportunities. This bookkeeping
defect is fixed; it does not guarantee that the original symmetric traffic will
produce a stable two-frame candidate on another run.

## Per-stage table

Times below are IST on October 23, 2026. A = h1/h4, B = h3/h6. Path notation
1254 means s1→s2→s5→s4. **Baseline and installed routes are A=1254, B=3256 in
every row**, including after hard refreshes. Recommendation sets include all
paths observed during the stage; they are not installed changes.

Utilization ranges are percentages of the configured capacity. M covers the
five loaded links (s1-s2, s2-s3, s2-s5, s4-s5, s5-s6); P is the RF forecast on
the shared s2-s5 backbone. Full measured/predicted ranges for every link are in
the JSON, including the idle s1-s6 and s3-s4 alternatives. Five-second samples
at boundaries can include the previous traffic regime.

| Stage / rate / start | M % | P % | Baseline A/B | Recommended A; B | Installed A/B | Outcome / blocker |
| --- | --- | --- | --- | --- | --- | --- |
| 0 / 7M / 13:49:27 | 0–7.2 | 12.2–28.7 | 1254/3256 | 1254; 3256 | 1254/3256 | Baselines remain optimal; no candidate. |
| 1 / 45M / 13:50:08 | 44.8–46.3 | 34.0–52.8 | 1254/3256 | 1254,1654; 3256,3456 | 1254/3256 | Alternatives appear; gain below 10%, forecasts below 80%. |
| 2 / 60M / 13:50:28 | 59.2–61.8 | 54.4–71.7 | 1254/3256 | 1254,1654; 3256,3456 | 1254/3256 | Some gains exceed 10%, but no installed-path forecast reaches 80%. Other frames have insufficient gain. |
| 3 / 70M / 13:50:45 | 68.9–72.0 | 80.3–82.8 | 1254/3256 | 1234,1654; 3216,3456 | 1254/3256 | Eligible candidates alternate every frame; confirmation remains 1. Four other pairs successfully reroute. |
| 4 / 80M / 13:51:00 | 78.5–82.3 | 88.0–94.4 | 1254/3256 | 1234,1654; 3216,3456 | 1254/3256 | One further alternating candidate; same-path refresh then restarts cooldown. |
| 5 / 95M / 13:51:21 | 92.4–97.8 | 55.6–85.6 | 1254/3256 | 1234,1654; 3216,3456 | 1254/3256 | Early cooldown, then insufficient gain or forecast below 80%. |
| 6 / 20M / 13:51:57 | 20.6–32.7 | 32.2–41.6 | 1254/3256 | 1234,1254; 3216,3256 | 1254/3256 | Residual recommendation has insufficient gain; baseline returns. |
| 7 / 7M / 13:52:19 | 7.2–9.6 | 26.9–63.9 | 1254/3256 | 1254,1654; 3256,3456 | 1254/3256 | Mostly baseline; isolated alternative lacks threshold/gain. |
| 8 / 90M / 13:52:39 | 69.6–92.6 | 35.7–71.7 | 1254/3256 | 1254,1654; 3256,3456 | 1254/3256 | Low forecasts/gain; another same-path refresh imposes cooldown. |
| 9 / 95M / 13:53:10 | 91.5–97.8 | 59.2–91.0 | 1254/3256 | 1234,1654; 3216,3456 | 1254/3256 | Early cooldown, then forecast/gain failures; no eligible logged candidate. |

Rejection descriptions are reconstructed from saved weights and the policy's
branch order. The controller does not persist a rejection event for each frame;
the JSON's retrospective counts must not be interpreted as exact executed
branch counts, especially during transactions or when an old recommendation
survives into the next stage. Logged candidate counts are authoritative.

## Predictions, weights and decision timeline

The database contains 406 observations and 406 predictions: 336 predictions are
valid, 21 insufficient-history records precede warmup, and remaining records
cover initialization/teardown invalidation. Fully valid fresh snapshots show
prediction ages 0.023–5.982 seconds, inside the ten-second limit, with the saved
model `rf-b6f2c2909490`. Predictions are for the real 15-second horizon.

Weights do change substantially. For example, s1-s2 goes from roughly 5.01–5.15
in stage 0 to 79.68–104.79 in stage 4; s2-s5 goes from 2.01–2.34 to
31.95–41.75. The unchanged formula is base_delay × (1 + 25u⁴).
Dijkstra recommends alternatives from stage 1 onward; lack of RF inference or
weight updates is not the blocker.

At 13:50:49.877–13:51:05.927 IST the tracked candidates are:

| Time IST | A recommendation / gain / count | B recommendation / gain / count |
| --- | --- | --- |
| 13:50:49 | 1654 / 13.055% / 1 | 3456 / 13.309% / 1 |
| 13:50:54 | 1234 / 13.266% / 1 | 3216 / 13.023% / 1 |
| 13:50:59 | 1654 / 13.566% / 1 | 3456 / 13.527% / 1 |
| 13:51:05 | 1234 / 14.224% / 1 | 3216 / 14.242% / 1 |

These gains exceed 10% and installed-path forecasts exceed 80%. It is the
identity of the candidate path that changes, resetting confirmation, not an
inability to find any useful alternative. At 13:50:54.899–.904, h1/h5, h2/h6,
h3/h5 and h2/h4 each activate their second-confirmation candidate with reason
`predicted_congestion` and verification `openflow_flow_stats`.

For A, same-path refreshes activate at 13:51:06.948 and 13:52:50.260. The old
implementation updates `last_change` on every activation, including these
maintenance operations, creating inappropriate cooldown windows until about
13:51:36.948 and 13:53:20.260. B behaves similarly. Genuine path changes should
still start the full cooldown.

There are **zero installation_failed, quarantine, flow-verification-mismatch or
baseline-recovery events** in this run. All twenty UDP clients complete with
valid sender/receiver results. At 95 Mbps, actual reception is approximately
94.93 Mbps per client; loss is zero except a negligible stage-4 h1 loss. There
is no recurrence of the earlier forwarding interruption.

## Traffic placement and acceptance scope

The two equal-rate flows load all five active links nearly equally in normalized
utilization: the 200-Mbps s2-s5 backbone carries twice the load of each 100-Mbps
outer link. At 2×95 Mbps this is approximately 95% utilization on all five.
Consequently, A's alternatives retain either the loaded source-side s1-s2 or
the loaded destination-side s4-s5; B has the corresponding symmetry. Tiny RF
differences swap the cheapest escape path. Measured high utilization is real,
although successful shaping/delivery does not imply persistent queue loss.

The acceptance observer intentionally searches only A and B, then requires a
different installed path, positive per-hop exact-cookie counters, host delivery
and independent OVS dumps. Neither pair ever installs a predicted-congestion
alternative in this recording. The script correctly fails this scoped check;
the four other verified rule installations are not proof of redirected live
UDP delivery for A/B. Broadening the search to idle pairs would weaken that
test. No acceptance-script change is needed for this failure.

## Correction and regression

`predictive_routing.py`: preserve the routing cooldown timestamp only when
activating an existing path for `refresh_hard_timeout`. Initial installation,
recovery, and real path changes retain their previous cooldown semantics.
The 10% improvement requirement, two identical confirmations, congestion
threshold, model, weight formula, readback and quarantine are unchanged.

`tests/test_predictive_routing.py`: a new regression initially failed because
refresh changed `last_change` from 100 to 201. With the fix it preserves that
timestamp and allows a genuine two-frame congestion-driven route change after
refresh. Existing tests still verify cooldown after real changes.

Executed verification: focused routing tests 25/25 passed; full suite 157
discovered, 139 passed, 18 Mininet-dependent skips (68.850 seconds). System
Python lifecycle/launcher tests passed 15/15 and topology/teardown tests passed
6/6. `git diff --check` passed. No live validation was executed.

## Next targeted manual experiment

Use an asymmetric bottleneck: a persistent 5-Mbps h1→h4 probe plus a ramped
h1→h2 cross-flow. Their combined host-access load remains below 100 Mbps.
This loads s1-s2 strongly while leaving s4-s5 and s2-s5 lightly loaded, giving
A a stable, substantially cheaper escape via s1-s6-s5-s4. The cross-flow can
also reroute in response to genuine predictions, so record its actual behavior
rather than assuming fixed placement. No scenario can promise a particular RF
forecast; failure to trigger is diagnostic evidence, not permission to inject
one.

Start the existing controller manually in terminal 1 (use a fresh directory):

```bash
cd /home/bob_the_bob/Flowrider
mkdir -m 700 data/phase5-targeted-manual
~/.pyenv/versions/3.10.16/bin/python -u controller.py \
  --ofp-listen-host 127.0.0.1 --ofp-tcp-listen-port 16653 \
  --telemetry-enabled --telemetry-db "$PWD/data/phase5-targeted-manual/telemetry.sqlite" \
  --telemetry-model data/research-20261015T180515Z/evaluation/model.pkl \
  --predictive-routing > data/phase5-targeted-manual/controller.log 2>&1
```

Terminal 2, the recommended next manual verification command:

```bash
cd /home/bob_the_bob/Flowrider
sudo /usr/bin/python3 mininet_topology.py --controller-port 16653 --bandwidth-scale .1
```

In the Mininet CLI, start servers and the probe, warm up 60 seconds, then ramp
the cross-flow. The foreground clients enforce each ramp duration:

```text
h4 iperf3 -s -p 5201 > data/phase5-targeted-manual/h4-server.log 2>&1 & echo $! > data/phase5-targeted-manual/h4-server.pid
h2 iperf3 -s -p 5201 > data/phase5-targeted-manual/h2-server.log 2>&1 & echo $! > data/phase5-targeted-manual/h2-server.pid
h1 iperf3 -c 10.0.0.4 -u -b 5M -t 240 -J > data/phase5-targeted-manual/probe.json 2> data/phase5-targeted-manual/probe.stderr &
sh sleep 60
h1 iperf3 -c 10.0.0.2 -u -b 45M -t 15 -J > data/phase5-targeted-manual/cross45.json
h1 iperf3 -c 10.0.0.2 -u -b 70M -t 15 -J > data/phase5-targeted-manual/cross70.json
h1 iperf3 -c 10.0.0.2 -u -b 90M -t 60 -J > data/phase5-targeted-manual/cross90.json
h1 ping -c 5 10.0.0.4
```

During the probe, inspect `routing_state.py <database> --events 100`, confirm
A's actual predicted-congestion activation, and dump flows on its installed
path using `sudo ovs-ofctl -O OpenFlow13 dump-flows sN`. Require the same cookie,
correct VLAN/actions and increasing h1→h4 packet counts on every hop, alongside
successful receiver JSON and ping. This is targeted diagnosis, not the complete
ten-stage acceptance. Finish the probe and stop the two owned iperf servers
with SIGINT using their recorded PIDs before exiting Mininet; then stop the
owned controller. Do not use global cleanup commands.
