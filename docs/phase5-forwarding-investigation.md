# Phase 5 Stage 0 forwarding investigation

Evidence: `data/phase5-20261023T081009Z`, read without modifying the recording.
Machine-readable extracted events, verified counters, link observations and file
checksums are in `phase5-forwarding-evidence.json`.

The interruption starts with independent reverse-direction idle expiration on a
bidirectional route. Recovery then fails exact readback of the ingress drop
guards, quarantines the pair, and keeps dropping traffic on every cleanup retry.
The empty APPLY_ACTIONS versus absent-instructions representation of drop
reproduces this quarantine failure in a regression. Raw guard replies and raw
FlowRemoved bodies were not saved, so the precise failing guard field and which
switch reported expiration first cannot be independently recovered from this
recording. New mismatch events retain expected rules and actual reply fields.

## Timeline

The following timestamps are the original recorded UTC times, on October 23,
2026; add 05:30 for IST (08:10:18 UTC is 13:40:18 IST).

| UTC | Observation |
| --- | --- |
| 08:10:18.474635 | Begin baseline h3/h6 installation: path `[3,2,5,6]`, VLAN 12, cookie `0x4652000000000018`. |
| 08:10:18.484294 | Activation after exact flow-stat verification of all eight rules, both directions. Initial pingall also passes 30/30. |
| 08:10:19.861609 | Stage 0 starts. h3 client launch is 08:10:19.863473; deadline is launch monotonic time +50 seconds. |
| 08:10:23.919125 | Verified reverse counters are 11 packets at each hop; forward counters already exceed 2,290. |
| 08:10:29–49 | Audits continue matching all eight rules. Reverse counters stay exactly 11; forward counters rise. At 08:10:49.099242, forward counts are 16,824/16,818/16,818/16,811 at s3/s2/s5/s6. |
| 08:10:51.439940 | `baseline_recovery`, reason `flow_expired`, same topology epoch 12. The reverse path has been quiet for about 30 seconds. |
| 08:10:51.443512 | `route_quarantined`, reason `flow_readback_mismatch`. Recovery orders guards, barrier, guard readback, then old-version deletion. This failure occurs at guard readback, before old-version cleanup. |
| 08:10:57.071335; 08:11:03.073483; 08:11:09.214477 | Same mismatch/quarantine on cleanup retries; no successful reactivation before timeout. |
| ~08:11:00 | Configured 40-second sender phase ends. Server reception already stopped at about test second 30.36. |
| 08:11:12.140691 | Timeout diagnostics: TCP control socket is ESTABLISHED, with one unacknowledged byte in h3's Send-Q; UDP socket remains established. Pair is quarantined, no installed/observed route is claimed. |
| 08:11:12 onward | Client interrupted; servers/pings reaped; network stopped. Later epoch-13 activation belongs to teardown topology changes, not Stage 0 recovery. |

`s2-s3` telemetry confirms approximately 7.20 Mbps until 08:10:51, 0.60 Mbps
over the next sampling window, then zero. h6's server reports no data after
second 31, despite h3 sending its full 34,999,608 bytes. h1/h4 completes at
approximately 7 Mbps, and both continuous ping pairs have zero loss. All 126
prediction records have no positive congestion warning: this is not an
ML-selected path change or a weight-formula failure. iperf mutex errors occur
in the interrupted client's stderr; they do not explain the earlier forwarding
loss. Acceptance correctly fails and scoped cleanup reports success.

## Intended rules versus observed state

All eight version rules originally had priority 20, idle timeout 30 and hard
timeout 120. Readback counters use the same cookie above. Controller readback
checks cookie, table, priority, complete match, timeout values and actions
before publishing the observed counters. The recording contains that verified
state, but no independent `ovs-ofctl dump-flows` or raw reply body.

| Direction | Switch: in → out | VLAN/action intent |
| --- | --- | --- |
| h3→h6 | s3: 1→2; s2: 3→4; s5: 4→3; s6: 2→1 | Ingress untagged; push/set VLAN 12; tagged transit; pop at s6. |
| h6→h3 | s6: 1→2; s5: 3→4; s2: 4→3; s3: 2→1 | Ingress untagged; push/set VLAN 12; tagged transit; pop at s3. |
| Recovery guards | s3 and s6 host ingress, priority 40 | Untagged, exact source/destination MACs, drop. |

Positive counters in both directions rule out initially absent reverse rules
and an initial VLAN/output-port mistake. The 120-second hard expiration is not
due. Per-rule idle expiration does not represent inactivity of the pair: UDP
payload continues forward while the reverse TCP control direction is silent.
The old handler treats any matching-cookie expiration as loss of the entire
pair. The new timer regression reproduces reverse-rule disappearance at 40
seconds; it fails before the fix.

The guard regression returns OVS-style drop readback (`instructions=[]`) rather
than the controller's empty APPLY_ACTIONS instruction. Previously their wire
encodings differed despite identical drop semantics; verification quarantined
the pair and recovery never reached guard deletion. This reproduces the exact
failure phase and persistent quarantine sequence, with the raw-evidence limit
stated above. Verification continues rejecting nonempty output actions and
other instructions, including CLEAR_ACTIONS.

## Fix and validation

Only `predictive_routing.py` changes production behavior:

- Version rules use idle timeout zero. Their existing hard timeout of 120
  seconds and whole-route refresh at 100 seconds remain. This deliberately
  manages a bidirectional version as a unit; entries remain lifetime-bounded
  and the existing pair limit remains in force.
- Normalize only empty APPLY_ACTIONS in action comparison. All other readback
  fields/actions remain checked, and quarantine still operates on real errors.
- Missing/expired-rule repair preserves the selected path if it is still
  valid, using the existing guarded cleanup and verified reinstall. Invalid
  paths still fall back to baseline; ML evaluation remains unchanged.
- On readback mismatch, persist the phase, switch, intended rules and actual
  rule fields/instructions for subsequent diagnosis.

`tests/test_predictive_routing.py` adds failing-before-fix regressions for drop
readback recovery, reverse silence and selected-path repair. A timed packet
fabric test exercises actual push/set/pop/output actions in both directions
through reverse silence beyond 30 seconds and through hard refresh to 130
seconds, with unchanged path and no tag leak or loop. Normalization tests reject
non-drop behavior. The wrong-output regression now uses the configured timeout
so it tests actions rather than accidentally failing on a timeout mismatch.

Full automated suite: 156 discovered, 138 passed, 18 Mininet-dependent skips
under pyenv Python (62.287 seconds), including the real TCP controller/simulated
OpenFlow forwarding test. Focused routing regressions: 24/24 passed.
Lifecycle/launcher tests under system Python: 15/15 passed; Mininet topology and
teardown tests: 6/6 passed. `git diff --check` passed.
No full live validation was run, no iperf timeout changed,
and no model or weight formula changed. The system is ready for another manual
Mininet validation; simulated-switch results are not live acceptance.

```bash
cd /home/bob_the_bob/Flowrider
bash scripts/run_phase5_validation.sh
```
