# Phase 5: verified targeted live acceptance and demonstration readiness

The targeted live acceptance has **passed**, following the earlier manual h1↔h4
experiment. This does **not** establish that the full acceptance suite passes or
that predictive routing improves latency/throughput over an equivalent baseline.

## Latest targeted acceptance: PASS

Evidence: `data/phase5-20261023T091240Z/acceptance.json`, status
`passed_targeted_live_mininet`, source `live_mininet`, scenario `targeted`.
[Extracted results and original artifact hashes](phase5-targeted-acceptance.json)
preserve a concise record; the original evidence was not modified or rerun.

| Scenario | Confirmed result |
| --- | --- |
| Initial and final connectivity | Both `pingall` checks: 30/30 received, 0% loss |
| Offered cross-traffic | 7→45→70→85→7 Mbps h1→h2, with a 5-Mbps h1→h4 probe; all five stages and all ten clients passed |
| Genuine RF route | h1/h4 baseline `[1,2,5,4]` → installed `[1,6,5,4]`, reason `predicted_congestion` |
| OpenFlow evidence | Matching installed/observed cookie `0x465200000000001f`; positive counters on every hop; independent OVS dumps from s1/s6/s5/s4 |
| Host delivery | Applied-path ping passed; continuous h1→h4 and h2→h5 loss both 0%, no duplicates |
| Low-load recovery | Fresh s1-s2 measured utilization 0%, forecast approximately 0%, verified forwarding and ping on `[1,6,5,4]` |
| Scoped cleanup | `cleanup.json` and post-teardown preflight passed; `scoped_cleanup_verified=true` |
| Physical backbone failure/restoration | Not part of targeted acceptance; remains outstanding |

The recovery result verifies forwarding after load subsides; it does not imply
a forced return to baseline. No matched latency/goodput/loss benefit is claimed.

## Full acceptance: NOT RUN in this review

Mininet imports successfully and the controller port 16653 was available at
inspection. No conflicting `s1`–`s6`/`h1`–`h6` interface names were visible.
However, `sudo -n true` returned `sudo: a password is required`, and read-only
OVS bridge inspection was denied access to its database socket. Consequently
the full privileged experiment was not started. This is an execution limitation,
not a new full-suite failure. The older full run remains a historical failure.

The full procedure differs from targeted acceptance as follows:

| Check | Targeted | Full |
| --- | --- | --- |
| UDP pairs | h1→h2 cross-load and h1→h4 probe | h1→h4 and h3→h6, both at the stage rate |
| Traffic stages | Five asymmetric stages, 155 configured traffic seconds | Ten symmetric stages at 7,45,60,70,80,95,20,7,90,95 Mbps; 250 configured traffic seconds |
| Verified RF alternative | Must involve h1/h4 | Must involve h1/h4 or h3/h6 |
| Low-load assertion | Explicit fresh measurement/forecast and forwarding gate | Lower-rate stages, without the targeted explicit recovery gate |
| Backbone failure/restoration | Not run | Bring s2–s5 down, verify h2→h5 delivery, restore link, verify delivery |
| Common checks | Exact rule readback, positive hop counters, OVS dumps, traffic completion, ping loss/duplicates, final connectivity and scoped cleanup | Same |

An earlier full run completed traffic but failed its genuine-alternative gate
because symmetric loading caused candidate alternation. That risk still exists;
the targeted pass must not be substituted for the full suite's stricter result.
In the full script, the backbone test is reached only after the RF proof and
continuous-ping checks succeed.

After ensuring no other lab owns the configured switch/interface names, run in
an authenticated Ubuntu terminal:

```bash
cd /home/bob_the_bob/Flowrider
FLOWRIDER_SCENARIO=full bash scripts/run_phase5_validation.sh \
  data/research-20261015T180515Z/evaluation/model.pkl
```

The explicit environment value overrides any previously exported `targeted`
selection. Expect roughly five minutes including setup and verification.
Inspect the new evidence directory's `acceptance.json`; only status
`passed_live_mininet`, scenario `full`, and `backbone_failure_recovery=true`
establish a full pass. Preserve failed-run evidence if any gate fails.

### Safety review

The launcher checks the controller port before starting a controller and creates
a new timestamped evidence directory with `mkdir`, refusing an existing one.
Root preflight checks bridges and interfaces for reserved lab names before
constructing a network and refuses collisions instead of replacing them. It
checks these names again after teardown. The privileged preflight still needs
to execute in the manual run; the present environment could not inspect OVS.

Cleanup tracks only spawned controller/validator/client/server/ping PIDs and
this Mininet network's objects. OVS shutdown deletes only the supplied switch
bridges. There is no `mn -c`, global `pkill` or unrelated-controller shutdown.
Existing unowned iperf daemons were observed and left untouched. Do not run two
labs that use the same reserved names concurrently; preflight is a collision
check rather than a concurrency lock.

Rechecked without a live lab: 18 lifecycle/launcher tests and 6 topology/teardown
tests passed, including collision refusal, failure cleanup and signal handling.
Launcher shell syntax passed. No routing, frontend, model, threshold or topology
changes were needed during this review.

## Preserved experiment

The source is `data/phase5-manual/telemetry.sqlite`, session
`220fce7d4a3149d1888b86cb2006c563`, using the existing
`data/research-20261015T180515Z/evaluation/model.pkl` (`rf-b6f2c2909490`).
A consistent SQLite backup, including committed WAL records, is preserved at
`data/phase5-manual-preserved-20261023/telemetry.sqlite`. The active controller
and user's Mininet processes were left running and untouched. Selected original
records and the backup SHA-256 are in [phase5-manual-verification.json](phase5-manual-verification.json).
No separate manual controller log, ping log, iperf JSON or independent OVS dump
was found in that evidence directory; missing records were not reconstructed.

Times below are IST on October 23, 2026; the JSON retains original UTC timestamps.

| Evidence | Result | Provenance |
| --- | --- | --- |
| Traffic generation | h1→h2 85 Mbps UDP, plus background h1→h4 | User-reported command conditions; background rate/duration not saved |
| S1–S2 at 14:18:21.039 | Measured 90.5834%; predicted 90.6999%, warning YES, +15-second horizon | SQLite prediction record |
| Candidate at 14:18:21.066 | `[1,6,5,4]`, confirmation 1, predicted improvement 84.4703% | Routing event |
| Candidate at 14:18:27.348 | Same path, confirmation 2, improvement 83.8693% | Routing event |
| Activation at 14:18:27.386 | Baseline `[1,2,5,4]` → installed `[1,6,5,4]`; `predicted_congestion`; cookie `0x465200000000007b`, VLAN 18 | Exact OpenFlow-stat verification event |
| Readback at 14:19:41.537 | Same installed/observed cookie and path; forward counters s1=129,542, s6=7, s5=7, s4=7; all eight directional rules have positive counters | Persisted controller OpenFlow readback |
| Quoted S1 result | 129,535 packets / 192,942,387 bytes | Matching snapshot preserved in JSON |
| End-to-end ping | 5 sent, 5 received, 0% loss, mean RTT 30.681 ms | User-reported terminal result; no saved ping artifact |
| Low-load behavior | Recommendation later returns to `[1,2,5,4]` while installed/observed path remains `[1,6,5,4]` | Snapshot; hysteresis does not force a baseline return |

The S1 counter must **not** be interpreted as 193 MB carried on the alternate
path: downstream counters are much smaller, and ingress rule replacement can
retain existing counters. Positive counters across the complete alternate path
support real packet forwarding, but do not prove sustained high-rate alternate
UDP throughput. The controller's `end_to_end_delivery_verified` remains false;
the separate reported ping is host-delivery evidence, not a database field we
changed.

## Implementation review and earlier failures

The controller already has serialized OS-Ken events, RF inference, weighted
Dijkstra, 10% minimum gain, two identical confirmations, 30-second cooldown,
bidirectional VLAN-versioned rules, exact readback, audits and scoped recovery.
The saved successful experiment uses that pipeline; no forecasts were injected.

Earlier investigations remain applicable:

- [Forwarding interruption](phase5-forwarding-investigation.md): independent
  reverse-rule idle expiration and drop-rule readback handling were corrected;
  whole-version hard expiry/refresh and verification remain in force.
- [Ten-stage acceptance failure](phase5-rerouting-analysis.md): equal-rate
  traffic loaded both candidate escape directions symmetrically, causing
  alternating recommendations and confirmation resets. Four other host pairs
  rerouted; the two UDP pairs did not. Same-path refresh incorrectly restarted
  cooldown; that defect and its regression were fixed before this task.
- The validator correctly requires a genuinely installed alternate path with
  positive exact-cookie counters, host delivery and independent OVS dumps.
  Idle-pair rule installation alone is insufficient. No threshold or safety
  mechanism has been relaxed.

## Shorter repeatable live procedure

After stopping the user's current lab normally with Mininet `exit` and stopping
its controller, run from an authenticated Ubuntu terminal:

```bash
cd /home/bob_the_bob/Flowrider
FLOWRIDER_SCENARIO=targeted bash scripts/run_phase5_validation.sh \
  data/research-20261015T180515Z/evaluation/model.pkl
```

Do not run alongside an existing lab: the launcher refuses occupied ports and
existing lab names. It starts only its own controller/lab, preserves evidence,
and cleans up only its own processes. It does not retrain or inject predictions.

The targeted procedure takes approximately three minutes, with two concurrent
UDP flows per stage:

| Stage | h1→h2 cross-flow | h1→h4 probe | Duration |
| --- | --- | --- | --- |
| Warmup | 7 Mbps | 5 Mbps | 30 s |
| Ramp | 45 Mbps | 5 Mbps | 15 s |
| Ramp | 70 Mbps | 5 Mbps | 15 s |
| Congestion | 85 Mbps | 5 Mbps | 60 s |
| Recovery | 7 Mbps | 5 Mbps | 35 s |

This concentrates load on s1-s2, leaving a clear alternative for h1/h4. It
requires fresh RF-driven h1/h4 activation, non-baseline installation, exact rule
readback, positive per-hop counters, OVS dumps and ping delivery. The final
low-load gate requires fresh s1-s2 measured utilization below 20%, valid RF
forecast below 80%, another verified current path and successful host delivery.
An alternative may remain installed because of hysteresis; a baseline return
is not forced. Continuous ping summaries and all iperf sender/receiver
measurements are retained, including incomplete/failed stages.

Success is explicitly `passed_targeted_live_mininet`, with scenario `targeted`
and a separate `low-load-recovery.json`. Missing triggers or failed recovery
fail the run. **This targeted procedure passed in the latest evidence directory
above and was not repeated during this review.** It does not run the physical-backbone failure check.

The original full schedule remains available unchanged by default:

```bash
bash scripts/run_phase5_validation.sh
```

Its status remains separate: the latest executed full run did not pass final
acceptance. Neither command was executed automatically during this review.

## Dashboard and presentation

Start the dashboard against the actual controller database:

```bash
bash scripts/start_demo.sh live --live-db "$PWD/data/phase5-manual/telemetry.sqlite"
```

The existing topology/route preview and link forecast views remain. A separate
**Controller forwarding** panel for the selected host pair shows baseline,
controller recommendation, installed path, observed OpenFlow path, installation
reason, per-hop counters, readback time and recent routing events. Replay never
presents those as replay forwarding. Missing/failed/stale data cannot claim
current verification, and the panel does not issue controller operations.

OpenFlow cookies exceed JavaScript's safe integer range. `/api/routing` now adds
`cookie_hex` alongside existing numeric fields so exact installed/observed
version comparison survives JSON parsing. A regression reproduces missing
lossless cookies; browser checks cover distinct adjacent cookies, stale data,
API failure, live/replay separation and mobile layouts. UI fixtures are test
data and are never counted as live network evidence.

## Performance interpretation

| Quantity | Available result | Comparison limit |
| --- | --- | --- |
| RTT | User-reported 30.681 ms after reroute | No saved equivalent baseline RTT |
| Packet loss | User-reported ping 0%; positive bidirectional hop counters | No matched baseline/predictive loss experiment |
| Throughput | S1–S2 utilization 90.58% on a 100-Mbps link | Link load, not isolated alternate-flow goodput |
| Switching | Two confirmations, then ~38 ms from installation-start event to activation | Controller transaction interval, not measured outage duration |
| Configured delay | Baseline 12 ms; alternative 15 ms one-way | Static configuration, not measured queuing latency |

No speedup or congestion-avoidance percentage is claimed. A matched baseline
comparison would require a separate controller run with predictive routing
disabled, identical offered loads, fresh evidence and separate ping/iperf logs;
the currently running user's experiment was not interrupted to manufacture that
comparison. Existing replay/research metrics were not modified.

## Demonstration readiness

The successful manual route change and targeted acceptance are preserved, and
the dashboard exposes the controller's actual state. FlowRider is ready to
demonstrate the verified targeted scenario. Final full-suite sign-off remains
conditional on running the full procedure, including backbone failure and
restoration. Equivalent-load performance benefits remain unproven.
The saved RF was trained under baseline routing;
closed-loop distribution changes and prediction accuracy remain limitations.

## Checks executed during this change

- `~/.pyenv/versions/3.10.16/bin/python -m unittest discover -s tests -v`:
  160 discovered, 140 passed, 20 skipped (Ubuntu Mininet imports), 63.241 s.
- `/usr/bin/python3 -m unittest discover -s tests -p 'test_phase5_*.py'`:
  18/18 passed, including targeted flow rates, recovery rejection and launcher
  scope. Topology/teardown tests under system Python: 6/6 passed.
- The lossless-cookie API test and targeted-rate test failed before their
  respective fixes and passed afterward.
- `npm --prefix frontend run build`: passed (32 modules).
- Existing `frontend/smoke.mjs`: passed real API, replay controls, previews,
  live-unavailable handling and 320/768/1440 layouts without browser errors.
- New `frontend/routing-smoke.mjs`: passed UI fixture checks for actual/preview
  separation, adjacent-cookie mismatch, stale/error clearing and mobile layout.
  The installed Chromium 1243 binary and existing local audio library were used;
  no browser/dependency download or system installation was needed.
- A separate unmocked browser check against the actual manual database showed
  installed/observed `[1,6,5,4]` and the exact hex cookie. It correctly reported
  incomplete current positive counters rather than claiming host delivery.
- The preserved backup passed SQLite `quick_check` and SHA-256 verification.
- `bash -n scripts/run_phase5_validation.sh` and `git diff --check`: passed.

These automated tests are separate from the subsequently successful targeted
live run recorded above and do not establish baseline-versus-predictive benefits.

## Files changed in this pass

- `tests/integration_phase5.py`, `scripts/run_phase5_validation.sh`: optional
  targeted scenario, explicit scope and low-load recovery gate.
- `tests/test_phase5_lifecycle.py`, `tests/test_phase5_launcher.py`: rate,
  recovery and scenario regressions.
- `demo_server.py`, `tests/test_routing_integration.py`: additive lossless hex
  cookies and API regression.
- `frontend/src/RoutingPanel.jsx`, `frontend/src/App.jsx`,
  `frontend/src/style.css`, `frontend/routing-smoke.mjs`: live routing panel,
  accurate preview labels and browser checks.
- `README.md`, `docs/phase5.md`, `docs/phase5-manual-verification.md`,
  `docs/phase5-manual-verification.json`: current evidence, reproduction commands
  and limitations. Existing historical validation results remain unchanged.
