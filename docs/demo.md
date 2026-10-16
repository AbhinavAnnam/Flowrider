# FlowRider: three-minute demo

```bash
cd ~/Flowrider
bash scripts/start_demo.sh replay
```

Open **http://localhost:8080**. **Ctrl+C** stops only the dashboard/API.
No sudo, Mininet or controller is required for replay.

1. **First minute:** explain SDN and show the six-switch topology. Point out the
   **REPLAY / RECORDED DATA** label. Click **Reset → Next event**.
2. **Second minute:** inspect s2-s5, compare current and +15-second predictions,
   and explain the 80% threshold. Start/pause playback at 4×.
3. **Third minute:** compare cyan baseline policy and amber proposed route;
   explain `W = D₀ × (1 + 25 × Û⁴)`. Click **Recalculate preview**. State that
   ML routes are calculated only and have not been installed in OpenFlow.

Use real research results honestly: 850 matched live forecasts; RF live MAE
0.1660 versus persistence 0.1865. Alert F1 varies across independent sessions.
No proactive congestion-avoidance benefit has been proved.

For optional live startup, safe shutdown, the complete file explanation,
architecture diagram and 12 faculty questions, read [demo_review.md](demo_review.md).
