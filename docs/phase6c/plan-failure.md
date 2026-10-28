# Phase 6C: apparent plan mismatch after an RTT abort

Evidence: `data/phase6c-feasibility-fix-20261028-01/`, inspected using the
readable copy `/tmp/flowrider-phase6c-plan-debug/`. Its raw baseline trial is
`trials/01-baseline/`. Original evidence was not modified; all copy file hashes
matched before and after read-only inspection, including SQLite/WAL handling.
No live Mininet experiment was launched for this fix.

## Root cause

`docs/phase6b/analyze.py:148` raises `Actual stages differ from recorded plan`.
It already compares equivalent planned properties only: `scenario`, `rate_mbps`,
`seconds`, `reverse`, `planned_elapsed_seconds`. Actual wall timestamps,
monotonic elapsed offsets, counters and probe metadata are not part of that
comparison. The shared check is correct and has not been disabled or changed.

The mismatch is genuine **truncation**, not incompatible plan schemas: nine
stages were planned, but only four were recorded. The earlier failure is in
`mininet.log`: original `collect_phase6c.py:181` raises
`RuntimeError: Incomplete RTT measurement` after sustained stage 3.

Original `collect_phase6c.py:163` computed the remaining ping timeout from
`probe['started'] + seconds + probe_timeout + 2 - time.time()`.
Those are wall-clock timestamps. During the 45-second stage, wall time advanced
about 48.231 seconds while recorded monotonic elapsed time advanced about
45.153 seconds. The clock steps consumed the unchanged three-second completion
allowance. The collector then interrupted probes before natural completion:

- h1→h2: **224 transmitted**, expected 225; strict ping parsing correctly rejects it.
- h3→h4: 225 transmitted, but `RTT probe timed out` remains recorded and cleanup
  sent SIGINT. It is not a successful natural probe exit.

The collector exits 1 and never reaches stages 4–8. The validator subsequently
runs the complete-trial reader and replaces this actionable failure with the
stage-list mismatch. Controller exit -15 was requested normally; the model and
source hashes match. No controller traceback/error preceded the probe failure.

## Field-by-field comparison

The plan in `experiment.json`, `trial.json`, `plan.json`, and the SQLite session
configuration is identical. It exactly matches the current feasibility-plan
generator before this fix; **no schedule regeneration was necessary**.

| Property | Expected | Recorded |
| --- | --- | --- |
| Profile / seed | feasibility / 42 | Match in manifest/SQLite/plan; traffic seed 42 |
| Traffic stage count | 9 | 4; stages 4–8 absent after RTT abort |
| Order | low, buildup, buildup, sustained, four burst/gap stages, recovery | Correct first four-stage prefix |
| Source/destination pairs | h1→h2 and h3→h4 | Match for every recorded stage and iperf JSON connection |
| Per-flow Mbps | 1, 6, 12, 16, 18, 1, 18, 1, 1 | 1, 6, 12, 16; remaining stages not generated |
| Durations (s) | 25, 15, 15, 45, 15, 15, 15, 15, 25 | 25, 15, 15, 45; matching iperf durations |
| Planned start offsets (s) | 0, 25, 40, 55, 100, 115, 130, 145, 160 | 0, 25, 40, 55; exact match |
| Total planned traffic (s) | 185 | Requested 185; only 100 nominal traffic seconds generated |
| Telemetry tail | 20 s, outside traffic stages | Configured correctly; not reached after abort |

Measured timing is intentionally retained separately:

| Stage | Planned offset s | Actual collector monotonic offset s | Measured wall window s |
| --- | ---: | ---: | ---: |
| 0 | 0 | 1.002142 | 25.106413 |
| 1 | 25 | 26.111298 | 16.616233 |
| 2 | 40 | 41.216065 | 15.144082 |
| 3 | 55 | 56.365832 | 48.231168 |

Offsets include server startup, process/probe setup and result exchange.
They must not be equated to nominal planned offsets. The wall/monotonic
separation also exposes clock adjustments. The 15-second minimum and separate
20-second tail introduced previously did **not** cause the plan mismatch:
stage 3 is still the same 45-second sustained stage, and no tail became a
traffic stage.

## Changes and strict verification

- `collect_phase6c.py`: record monotonic probe/stage timing alongside original
  wall timestamps; calculate the **same** completion deadline using monotonic
  time. No timeout allowance, traffic duration, offered rate or probe count
  was increased.
- `phase6c.py`: use complete monotonic timing metadata for probe coverage on new
  recordings. Keep legacy wall-time validation for historical records. Missing
  timing fields, inadequate coverage, partial ping counts, and probe errors
  still fail. Stage count errors now state expected and recorded counts.
- `docs/phase6c/analyze.py`: reject aborted/truncated collections with their
  original collector and probe errors and an unavailable summary. Do not pass
  a partial run to complete-trial analysis or generate comparative metrics.
- `tests/test_phase6c_collector.py`: a test using the actual collector and the
  existing Phase 5 process lifecycle reproduces the exact RTT abort with a
  simulated forward wall-clock step. It now completes all nine planned stages
  in both routing modes, retains the separate 20-second tail, and still fails
  genuine delayed probes. No Mininet network is created.
- `tests/test_phase6c_analysis.py`: partial four-of-nine records reproduce the
  exact shared-reader exception; Phase 6C reporting retains the RTT root cause.
  Both modes accept complete records with extra measured timing fields while
  rejecting wrong order/scenario, planned offsets, reverse flag, rates,
  durations, endpoints and seed.
- `tests/test_phase6c.py`: clock coverage tests retain raw wall evidence and
  reject incomplete monotonic metadata or insufficient covered time.

The failure-reproducing tests were observed failing before implementation.
The original failed evidence remains rejected. The trained model SHA256,
`e279097186c5af01` six-switch default, routing policy, forwarding verification,
coverage thresholds, existing 15-second stage minimum and 20-second tail are
unchanged. No historical dataset, trial, or report was rewritten.

## Verification and rerun

Verification completed:

- Focused Phase 6C tests: 47 passed in the controller Python (including the
  new coverage case); the collector integration case passed separately under
  Ubuntu Python, for 48 focused cases covered.
- Existing baseline/predictive routing, saved-model integration and OpenFlow
  socket tests: 59 passed.
- Full controller suite: 221 discovered, 197 passed, 24 Mininet-specific skips,
  zero failures (85.194 seconds). All 24 skipped cases passed under Ubuntu
  Python: 14 lifecycle, 7 topology, 2 smoke and 1 collector case.
- Historical Phase 6B arithmetic/interpretation: 5 passed.
- Syntax compilation and `git diff --check`: passed.

Live success has not been re-established; the code is ready for a fresh
feasibility validation. From the repository root:

```bash
sudo /usr/bin/python3 scripts/run_phase6c.py \
  --profile feasibility --trials 2 \
  --experiment data/phase6c-feasibility-fix-20261028-02
```

The directory must be new. This runs only the baseline/ML feasibility pair;
no long ABBA benchmark was launched automatically. Genuine missing stages,
incomplete probes, forwarding failures, clock-related telemetry coverage gaps,
or traffic-plan differences will still reject a live rerun.
