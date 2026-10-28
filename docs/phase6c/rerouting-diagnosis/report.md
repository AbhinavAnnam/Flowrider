# Why Phase 6C did not activate ML detours

**Primary cause: the RF never produced the ≥80% forecast required to authorize
rerouting. Dijkstra did recommend detours; forwarding never received an ML
installation request.** This is a forecast calibration/transfer failure for
this experiment, not a demonstrated topology or OpenFlow defect.

Source: `data/phase6c-feasibility-fix-20261028-02/`, inspected through the
user-provided read-only diagnostic copy. Both trials pass existing validation.
Only analysis artifacts were added. No production code, model, policy, topology,
threshold, trial or historical report was changed; no live experiment was run.

## Installed paths and real congestion

Every one of the nine stages in **both modes** retained:

| Traffic | Verified installed path | Available detour |
| --- | --- | --- |
| h1→h2 | S1–S2–S4 | S1–S3–S4 |
| h3→h4 | S2–S4 | S2–S1–S3–S4 |

Per-stage proofs include increasing bidirectional counters and independent OVS
evidence. Brief hard-refresh cutover snapshots have `installed_route=null`; no
alternative path was marked installed. Both detours use active ports. Their shared
S1–S3/S3–S4 links have 100 Mbps capacity and peak measured traffic-window
utilization ≤0.000113% in either run. Maximum aggregate offered load is 36 Mbps,
so the detour has ample spare capacity even if both pairs switch.

S2–S4 is correctly mapped to **S2 port 3 (`s2-eth3`) / S4 port 2**. Applied TC
and telemetry both use **20 Mbps**; recorded utilization equals TX Mbps / 20,
not TX / the logical 200 Mbps. Netem drop deltas are 97,658 baseline and 97,633
ML. Congestion exposure is 85.005 versus 90.005 link-seconds. Genuine congestion
was established, while the installed policy remained effectively static.

## Forecasts and timing

Traffic-window measurements and +15-second forecasts from the ML run:

| Link | Measured utilization range | RF forecast range | Matched-target MAE |
| --- | --- | --- | ---: |
| S1–S2 | 0.136–18.530% | 0.970–61.236% | 0.16163 |
| S2–S4 | 1.366–100.010% | 18.184–73.697% | **0.41629** |
| S1–S3 | 0–0.000113% | <0.000049% | <0.000001 |
| S3–S4 | 0–0.000113% | <0.000037% | <0.000001 |

There are 35 matched forecasts per ML link. Bottleneck RMSE is 0.44595;
**all 18 mature ≥80% bottleneck targets were missed**. When current bottleneck
load was already ≥80%, forecasts averaged 61.058% (range 35.929–73.697%).
The baseline run shows the same failure: bottleneck maximum forecast 73.872%,
MAE 0.36475, and zero alerts for 17 congested future targets.

Matches are correctly timed at 14.9987–15.0027 seconds. The frozen artifact
reproduces recorded bottleneck forecasts from the existing feature builder.
Aggregate MAE 0.14448 masks the larger bottleneck error because two links are
idle. Zero qualifying warnings means no established advance-warning lead time.

## Reconstructed decisions

All **70 forecast-available measured-pair decision records** reproduce the
stored edge weights, Dijkstra recommendation and recommended path cost.
Costs use `D × (1 + 25 × prediction⁴)`; they are weight scores, not measured RTT.

For example, +63.475 seconds after ML traffic starts, S2–S4 is measured at
99.998%, but forecast at 73.495%; S1–S2 is forecast at 20.125%:

| Pair | Installed cost | Detour cost | Calculated improvement | Outcome |
| --- | ---: | ---: | ---: | --- |
| h1→h2 | 18.669985 | 10.000000 | 46.438% | Detour recommended; no ≥80% forecast |
| h3→h4 | 16.587972 | 12.082013 | 27.164% | Detour recommended; no ≥80% forecast |

| Reconstructed decision outcome | h1→h2 | h3→h4 |
| --- | ---: | ---: |
| Distinct decision frames | 38 | 38 |
| Initial insufficient-history fallback | 3 | 3 |
| Recommended existing path | 20 | 26 |
| Alternative recommended | **15** | **9** |
| Alternative clears 10% gain and cooldown; fails ≥80% gate | **15** | **7** |
| Alternative below 10% gain and below ≥80% gate | 0 | 2 |

These are decision frames, not installation attempts: h1/h3 had four/five
alternative episodes and eight/ten path recommendation changes including
returns, matching the saved total of 18. H1 had 12 consecutive detour
recommendations; recommendation instability is not its primary blocker.

All 24 alternative frames had expired cooldown, fresh forecasts, valid topology,
and no in-progress route transaction. The gate in `predictive_routing.py:468–469`
resets candidate/count before confirmation processing. Consequently:
**0 candidate events, 0 ML installation attempts, 0 failed verifications,
0 ML path changes**. Confirmations were never reached. Startup fallbacks do not
explain later inaction. Gate reasons are reconstructed from state and source,
not logged rejection events.

## Capacity shift and the smallest useful next step

Training capacities are 100/200 Mbps. Saturation at 20 Mbps presents a shifted
**joint feature distribution**: utilization history, absolute TX/RX Mbps and
capacity are all inputs. This is not incorrect utilization normalization.

Capacity alone is not a sufficient causal explanation. All 188 capacity splits
across 84 of the 100 trees use threshold 150 Mbps. Merely replacing feature 20
with 100 follows exactly the same branches and changes **none** of these live
history forecasts. In a separately labelled **offline sensitivity check**, scaling
TX/RX and capacity together to 100 or 200 Mbps produces only one ≥80% prediction
in each trial, during recovery; the matched future load is below 3%. There are
no consecutive bottleneck warnings. This is not a live scaled-network result,
and does not prove what the full proposed network would predict.

**Recommendation: no routing-policy correction is indicated.** Keep this
negative result. First review frozen-model calibration on matched held-out
features; do not lower gates, remove confirmations or substitute reactive load.

A separately versioned test could remove the capacity-domain mismatch: **100 Mbps
S2–S4, 200 Mbps S1–S2, both detour links and host accesses**, with the current
rates multiplied by five (sustained 80+80=160 Mbps; burst 90+90=180 Mbps).
The bottleneck would be oversubscribed and the detour remain below capacity;
100/200 Mbps capacities match training. Preserve the same delays and safety
policy. This is only an in-distribution *capacity* test, not a guarantee that
joint histories or alert calibration match training. The sensitivity evidence
warns that unchanged RF failures may persist. This adjustment is proposed only;
it has **not** been implemented or run and must not replace this experiment.

## Isolated control and audit artifacts

Two isolated unittest checks passed. With **synthetic 0.95** bottleneck forecasts
and 0.05 elsewhere, h1's costs are 44.725625 versus 10.001563; h3's are 42.725313
versus 12.001875. After the unchanged cooldown and two confirmations, the existing
engine installs/verifies each detour in the simulated OpenFlow fabric and sends
packets in both directions. These checks establish conditional routing behavior,
not real-model predictive skill or live rerouting. No injected forecast entered
any live trial, performance result or recorded prediction table.

`evidence/decisions.csv` contains every reconstructed measured-pair decision;
`installed-paths.csv` contains all 36 stage/pair records; `results.json` contains
per-link errors, gate counts and trial evidence; `synthetic-capacity-sensitivity.csv`
is explicitly offline; `input-manifest.json` fingerprints the preserved inputs.
Model SHA256 and six-switch source/default remain unchanged. RTT means
25.046/25.016 ms and identical 65.6 ms P95 occurred without ML path changes;
those small differences cannot be credited to predictive rerouting.

Reproduce the read-only analysis into a new output directory:

```bash
~/.pyenv/versions/3.10.16/bin/python docs/phase6c/rerouting-diagnosis/analyze.py \
  --experiment /tmp/flowrider-phase6c-rerouting-debug --output /tmp/flowrider-rerouting-audit-NEW
```

Reproduce the isolated synthetic control (no network/model changes):

```bash
~/.pyenv/versions/3.10.16/bin/python docs/phase6c/rerouting-diagnosis/test_controlled_forecast.py
```
