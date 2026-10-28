# Phase 6C feasibility failure: diagnosis and fix

Evidence: `data/phase6c-20261028T075743073076Z/feasibility/01-baseline/`.
The root-owned original was not modified. Inspection used the user-provided
readable copy `/tmp/flowrider-phase6c-debug-20261028T075743073076Z/`.
SQLite was inspected through the existing private DB/WAL reader, retaining
committed WAL records without changing evidence. Fingerprints before/after
analysis matched. No live Mininet trial or benchmark was rerun.

## Exact failure chain

1. All nine traffic stages completed. All 18 UDP clients reported passed;
   all 18 timestamped probes were complete. Initial/final connectivity was
   12/12. Applied/final TC records exist; process and scoped cleanup succeeded.
2. The collector had no saved forward or reverse proof for stages **4, 5, 7**.
   `mininet.log` ends with `collect_phase6c.py:212` (the original source), raising:
   `RuntimeError: Stage 4: missing forwarding evidence; Stage 5: missing forwarding evidence; Stage 7: missing forwarding evidence`.
   This explains collector exit 1. Controller exit -15 was the requested shutdown.
3. The analyzer records those missing proofs, constructs `route={}`, then calls
   `verified_dump`. At **original `docs/phase6c/analyze.py:21`**, the expression
   `route['installed_route']` raises the reported `KeyError`. This secondary
   exception hides the collector's actionable validation errors.

The cause is **missing measurement evidence after an earlier collector failure**,
not a schema mismatch or missing route activation. All 373 SQLite snapshots use
the expected `installed_route` / `observed_route` route schema. The database has
54 routing events, including six initial activations and six same-path hard
refresh activations verified by `openflow_flow_stats`; there were no recorded
installation failures or quarantines. Independent OVS dumps show positive
counters on the installed paths during the affected stages. That does not
retroactively supply the missing saved per-stage counter-increase proof.

## Why the collector missed the proof

The original five-second burst/gap stages were too short for the unchanged
five-second audit policy once tick phase and a hard refresh were included.
`prior_counts` was reset at every stage (original collector line 112), and the
entire comparison was gated by `forwarding_proof` (original line 120), which
requires every hop's counter to be positive. A valid zero readback after refresh
was therefore discarded instead of establishing the initial counter values.
The first later positive readback merely established a baseline; another audit
was needed to show growth.

Actual SQLite timing relative to each stage start:

| Stage | Recorded duration | Relevant audit evidence |
| --- | ---: | --- |
| 4 | 5.313 s | Old-version audit at −1.89 s; refreshed-cookie readback at +3.13 s includes zero transit counters; no later positive controller readback before stage end |
| 5 | 6.587 s | Initial refreshed-cookie readback still contains zeros; first positive readback at +5.28 s; the old gate makes this the comparison baseline, leaving no later increase |
| 7 | 5.095 s | Previous audit at −0.63 s; next audit at +5.37 s, after the traffic window |

The same-path refresh was correctly verified about 3.13 seconds into stage 4;
it did not restart routing cooldown or represent congestion rerouting.

The now-visible validation also identifies insufficient telemetry coverage:
93.09% overall versus the unchanged 95% requirement, and short-stage/final-stage
coverage failures. Teardown replaced the next final-interval samples with
inactive records. The recording also contains roughly 1.5-second wall-clock
jumps relative to monotonic time, which reduce covered intervals; timing and
coverage thresholds have not been relaxed.

## Narrow changes

- `docs/phase6c/analyze.py`: missing, null, or empty installed routes return
  **false** from independent dump verification. Missing evidence remains an
  acceptance failure, with the original diagnostics visible instead of a crash.
- `collect_phase6c.py`: the extracted `record_forwarding` bookkeeping accepts
  complete, same-cookie nonnegative readbacks as comparison baselines, including
  verified zeros. Success still requires positive controller readback and
  **strict increases on every hop**, separately in both directions. Missing
  hops, wrong cookies and unchanged counters do not pass. Independent OVS
  verification remains required.
- `phase6c.py`: measurement contract 2 makes stages at least **15 seconds**,
  retaining multiple audit intervals and refresh margin. Full traffic lasts
  290 seconds; feasibility lasts 185 seconds. A **20-second telemetry tail**
  holds the network connected after traffic so final intervals and +15-second
  targets mature before teardown. Rates, topology, capacities, queues, probe
  methods, RF, horizon, weights, confirmation/cooldown, and forwarding policy
  are unchanged. Conditions remain identical between routing modes. Contract 1
  is still accepted for historical analysis, including the failed 135-second plan.
- `tests/test_phase6c_analysis.py`, `tests/test_phase6c_harness.py`,
  `tests/test_phase6c_four_switch.py`: regression coverage described below.
- `docs/phase6c/README.md`: corrected durations and measurement-tail instructions.

The sampling-window change is required to collect the existing strict proof;
it is not a relaxation of verification or a search for a favorable ML outcome.

## Regression proof and verification

Tests were written and observed failing before the corresponding fixes:

- Missing stage proof reproduced the exact `KeyError` for **both baseline and
  predictive ML**. Missing/null/empty routes also raised or incorrectly passed
  an empty-path loop. They now fail verification without masking diagnostics.
- Zero counters cannot prove forwarding, but a later positive increase on the
  same cookie can. A timed mid-stage refresh fixture requires collection after
  the next audit. Missing hops and wrong-cookie readbacks cannot seed evidence.
- Plans must provide multiple unchanged audit intervals per stage and a
  +15-second-horizon-plus-poll tail. Historical contract 1 still validates.

Focused Phase 6C tests: **43 passed** in the controller Python.
Ubuntu collector/harness tests: **11 passed**. Phase 5 lifecycle tests:
**14 passed**. Existing routing policy/integration/mode and baseline/predictive
OpenFlow socket tests: **59 passed**, including actual saved-model integration
and simulated bidirectional forwarding. These are automated tests, not live
Mininet performance validation.

The full controller-Python suite discovered **216 tests**: **193 passed**,
23 Mininet-specific cases skipped, zero failures (83.864 s). Those 23 cases
passed separately under Ubuntu Python: 14 lifecycle, 7 topology and 2 smoke
checks. Syntax compilation and `git diff --check` passed. The failed recorded
trial remains rejected with its original forwarding/coverage diagnostics; the
copy file hashes, saved-model SHA256 and historical topology identity were
checked unchanged.

## Fresh rerun

The code is ready for a new **feasibility validation**; a successful live rerun
has not been established. The failed trial remains failed. Strict coverage and
forwarding checks can still reject a rerun if timing or evidence is inadequate.
Do not resume the old source-hashed experiment or overwrite its outputs.

From `/home/bob_the_bob/Flowrider`:

```bash
sudo /usr/bin/python3 scripts/run_phase6c.py \
  --profile feasibility --trials 2 \
  --experiment data/phase6c-feasibility-fix-20261028-01
```

This runs only the baseline/ML feasibility pair. The named directory must be new;
the runner refuses existing outputs. No long ABBA run has been launched here.
