# Phase 6C automated verification

Verified October 28, 2026. Privileged Mininet performance trials were not run.

| Verification | Actual result |
| --- | --- |
| Full controller-Python suite: `~/.pyenv/versions/3.10.16/bin/python -m unittest discover -s tests -q` | 209 discovered; 186 passed, 23 skipped for Mininet; no failures (87.837 s) |
| Ubuntu Mininet/lifecycle tests: `/usr/bin/python3 -m unittest discover -s tests -p 'test_phase5_*.py' -q` | 18 passed, including all 14 lifecycle cases skipped above |
| Ubuntu topology tests: `/usr/bin/python3 -m unittest discover -s tests -p test_mininet_topology.py -q` | 7 passed, including the four-switch profile |
| Ubuntu Phase 6 smoke boundaries: `/usr/bin/python3 -m unittest discover -s tests -p test_phase6_smoke.py -q` | 2 passed |
| Ubuntu Phase 6C launcher/TC boundaries: `/usr/bin/python3 -m unittest discover -s tests -p test_phase6c_harness.py -q` | 8 passed |
| Historical Phase 6B analysis: `/usr/bin/python3 -m unittest discover -s docs/phase6b -p test_analysis.py -q` | 5 passed |
| `git diff --check` and compilation of the changed controller/topology/benchmark/analysis modules | Passed |

All 23 cases skipped by the controller Python passed under Ubuntu Python.
The 36 Phase 6C cases are included in the full suite. Lifecycle timeout/error
messages printed by guard tests are expected test fixtures, not live trials.

Coverage includes deterministic topology selection and ports, 4 ms/10 ms path
arithmetic, 20/100 Mbps capacities, 100 Mbps access, queue sizes, seeded schedules,
ABBA order, feasibility-before-full orchestration, failed-feasibility blocking,
immutable output directories, bounded process cleanup, host-namespace TC,
baseline fixed weights, unchanged ML weights/confirmations, bidirectional
OpenFlow rule and packet behavior, ping statistics/all-loss/missing samples,
telemetry coverage, independent forward/reverse counters, snapshot-versus-TC
consistency, congestion/spare-path evidence and immutable analysis inputs.

Test-first failures observed in this implementation include the missing
four-switch contract API, missing one-command launcher/profile support, missing
four-switch packet-fabric support, division by zero for zero expected probes,
and acceptance of incorrect controller delay weights despite correct applied
TC. Regression tests now pass for these cases. Integrating the established
Phase 3 evaluation also exposed incomplete synthetic prediction fixtures;
fixtures now retain the required threshold, raw prediction and target fields.
No checks were weakened to accept missing performance data.

Real OS-Ken subprocess tests verify selected-topology snapshots and SQLite
metadata without Mininet. Deterministic OpenFlow tests verify the conditional
ML reroute path and bidirectional packets using explicitly synthetic forecasts.
The existing saved RF itself is loaded and exercised through the existing
feature builder/Predictor, with its SHA256 checked before and after. These
checks establish schema compatibility and forwarding behavior, not live RF
transfer accuracy or a performance benefit.

Temporary paired four-switch and full ABBA fixtures exercise real SQLite/JSON
analysis, generate all eight graph files, and verify the raw input hashes are
unchanged. Fixture outputs are deleted with their temporary directories; no
synthetic graphs are presented as measured network results.

The historical six-switch version remains `e279097186c5af01`. Required binaries
(`ovs-vsctl`, `ovs-ofctl`, `tc`, `ip`, `iperf3`, `ping`) and Ubuntu Mininet are
installed; `openvswitch-switch` is active. No new dependency was installed.
The saved RF, earlier datasets and historical experiments were not modified.
Existing uncommitted project work was retained.

Actual feasibility attempt:

```text
/usr/bin/python3 scripts/run_phase6c.py --profile feasibility --trials 2
sudo: a password is required
Authenticated sudo unavailable. Run: sudo /usr/bin/python3 scripts/run_phase6c.py --trials 4
```

The command exits before experiment/network creation. Sudo authentication was
not bypassed. Live applied TC, congestion, RF-triggered installed changes, and
baseline-versus-ML numerical performance remain unverified. Run the single
command in [README.md](README.md) to collect them. Current findings, including
the model's out-of-training-range 20 Mbps capacity, are in [report.md](report.md).
