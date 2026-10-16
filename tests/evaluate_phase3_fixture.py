"""Reproducible SYNTHETIC mechanical evaluation, never a live-network result."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from traffic_engine import train_live_predictor


def run():
    records = []
    for session, seed, count, hour in [('synthetic-train-seed17', 17, 300, 0),
                                      ('synthetic-holdout-seed29', 29, 180, 1)]:
        generator, utilization = random.Random(seed), 0.4
        start = datetime(2026, 10, 15, hour, tzinfo=timezone.utc)
        for index in range(count):
            draw = generator.random()
            if draw < 0.05:
                utilization = generator.uniform(0.85, 0.99)
            elif draw < 0.08:
                utilization = generator.uniform(0.1, 0.3)
            else:
                utilization = min(1.05, max(0.01,
                    0.97 * utilization + 0.03 * 0.5 + generator.gauss(0, 0.025)))
            records.append(dict(session_id=session, source='synthetic', schema_version=1,
                observation_timestamp=(start + timedelta(seconds=index * 5)).isoformat(),
                monotonic_elapsed_time=index * 5, link_id='s2-s5', capacity_mbps=200,
                utilization=utilization, tx_mbps=200 * utilization, rx_mbps=10,
                valid=True, link_active=True, status='valid'))
    with tempfile.TemporaryDirectory() as directory:
        return train_live_predictor(records, Path(directory) / 'synthetic-only.pkl',
            source='synthetic', holdout_session='synthetic-holdout-seed29')


if __name__ == '__main__':
    print(json.dumps(dict(scope='Synthetic only; not real network forecast skill',
                          report=run()), indent=2, allow_nan=False))
