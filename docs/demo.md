# FlowRider faculty demonstration

Follow the [fresh-clone setup](../README.md#fresh-clone-dashboard-setup), then:

```bash
bash scripts/start_demo.sh replay --port 8081
```

Open **http://localhost:8081**. Ctrl+C stops only the API/dashboard. Neither
Mininet nor sudo is needed. All displayed times are IST; original UTC timestamps
and rule cookies remain available in the event details and portable source data.

1. **Performance Comparison:** show the four validated ABBA trials, two per mode.
   Discuss RTT, P95, loss and goodput. Retain the red warning: congestion exposure
   increased under ML. Results are descriptive, not statistical significance.
2. **Network Topology / Predictive Routing:** select trial 02 or 03 and show the
   six-switch fabric, fixed-delay baseline, RF forecasts and 80% gate. Click
   **Next verified reroute** to select actual saved path changes and matching
   installed-cookie snapshots. Explain the old/new path, reason, timestamp and
   positive counters in Event Timeline. Amber recommendations are not installed
   paths; independent OVS proof can have a later timestamp than the selected
   installation snapshot.
3. **Link Failure & Recovery:** select **Before Failure**, **Link Down /
   Rerouting**, and **Link Restored**. Inspect S2–S5, topology epoch, affected
   h2→h5, invalidation and the recorded `[2,1,6,5]` alternative. Keep the detailed
   slider/history. These are real Phase 5 snapshots and rule readbacks; missing
   positive per-hop counter evidence for the temporary routes is disclosed.
4. **Phase 3 replay:** demonstrate Start/Pause/Reset/Next event independently.
   These historical calculated previews are separate from Phase 6C forwarding
   evidence. Live readback needs an active controller database and never starts
   a privileged network operation.

If an asset is missing or invalid, restore the tracked file; do not substitute
fixtures or regenerate results. The required files are
`data/demo-dashboard/six-switch.json` and `data/demo-replay.sqlite`.

[The original Phase 3 review](demo_review.md) is retained as historical context;
its earlier capability limitations are not the current six-switch acceptance status.
