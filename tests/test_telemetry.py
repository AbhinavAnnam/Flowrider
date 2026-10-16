import tempfile
import unittest
from pathlib import Path

from telemetry import PortSampler, link_snapshot, aggregate_link
from telemetry_store import TelemetryStore, read_rows
from topology_state import TopologyState
from test_topology_state import activate


def counters(byte_count=0, **updates):
    values = dict(tx_bytes=byte_count, rx_bytes=byte_count, tx_packets=10,
                  rx_packets=10, tx_dropped=0, rx_dropped=0, tx_errors=0, rx_errors=0)
    return dict(values, **updates)


class SamplingTests(unittest.TestCase):
    def setUp(self):
        self.sampler = PortSampler()

    def sample(self, value, time, **kwargs):
        return self.sampler.sample((2, 4), value, time, capacity=200, token='connection1', **kwargs)

    def test_bytes_to_mbps_uses_elapsed_time_and_scaled_capacity(self):
        self.assertEqual(self.sample(counters(), 0)['status'], 'initial')
        result = self.sample(counters(100_000_000), 4)
        self.assertAlmostEqual(result['tx_mbps'], 200)
        self.assertEqual(result['utilization'], 1)

    def test_above_capacity_retains_raw_values(self):
        self.sample(counters(), 0)
        result = self.sample(counters(200_000_000), 5)
        self.assertEqual(result['status'], 'capacity_exceeded')
        self.assertTrue(result['valid'])
        self.assertEqual(result['utilization'], 1.6)
        self.assertEqual(result['operational_utilization'], 1)

    def test_reset_missing_and_recovery_never_become_zero_samples(self):
        self.sample(counters(100), 0)
        reset = self.sample(counters(10), 5)
        self.assertEqual(reset['status'], 'counter_reset')
        self.assertIsNone(reset['utilization'])
        self.assertEqual(self.sample(None, 10)['status'], 'missing')
        self.assertEqual(self.sample(counters(1000), 15)['status'], 'initial')

    def test_invalid_elapsed_stale_and_large_gap(self):
        self.sample(counters(), 5)
        for now in (5, 4):
            self.assertEqual(self.sample(counters(100), now)['status'], 'invalid_elapsed')
        self.assertEqual(self.sample(counters(100), 20)['status'], 'sampling_gap')

    def test_inactive_and_new_connection_reset_baseline(self):
        self.sample(counters(), 0)
        self.assertEqual(self.sample(counters(100), 5, active=False)['status'], 'inactive')
        self.assertEqual(self.sample(counters(200), 10)['status'], 'initial')
        result = self.sampler.sample((2, 4), counters(300), 15, capacity=200, token='new')
        self.assertEqual(result['status'], 'initial')

    def test_optional_unsupported_counters_stay_null(self):
        self.sample(counters(tx_dropped=None), 0)
        result = self.sample(counters(100, tx_dropped=None), 5)
        self.assertIsNone(result['packet_drop_rate'])
        self.assertEqual(result['port_error_rate'], 0)

    def test_nonfinite_time_and_stale_switch_duration_are_not_valid(self):
        self.assertEqual(self.sample(counters(), float('nan'))['status'], 'invalid_elapsed')
        self.sample(counters(), 0, duration=10)
        result = self.sample(counters(100), 5, duration=10)
        self.assertEqual(result['status'], 'stale_counters')
        self.assertFalse(result['valid'])

    def test_openflow_unsupported_byte_sentinel_is_missing(self):
        result = self.sample(counters(tx_bytes=(1 << 64) - 1), 0)
        self.assertFalse(result['valid'])
        self.assertIsNone(result['counters']['tx_bytes'])

    def test_snapshot_distinguishes_hosts_and_scaled_link_ports(self):
        state = TopologyState()
        activate(state)
        links, ports = link_snapshot(state, {i: object() for i in state.switches}, 0.1)
        self.assertEqual(next(l for l in links if l['link_id'] == 's2-s5')['capacity_mbps'], 200)
        self.assertEqual(ports[2, 1]['kind'], 'host')
        self.assertEqual(ports[2, 4]['kind'], 'inter_switch')
        state.unregister_switch(2)
        links, _ = link_snapshot(state, {}, 0.1)
        self.assertFalse(next(l for l in links if l['link_id'] == 's2-s5')['active'])

    def test_aggregate_uses_maximum_tx_direction_without_double_counting(self):
        directions = [dict(valid=True, utilization=0.6, tx_mbps=120, rx_mbps=30, monotonic_time=5),
                      dict(valid=True, utilization=0.15, tx_mbps=30, rx_mbps=120, monotonic_time=5.1)]
        result = aggregate_link(directions, active=True)
        self.assertAlmostEqual(result['utilization'], 0.6)
        directions[1]['valid'] = False
        self.assertIsNone(aggregate_link(directions, active=True)['utilization'])
        self.assertIsNone(aggregate_link(directions, active=False)['utilization'])


class StorageTests(unittest.TestCase):
    def test_atomic_sessions_schema_invalid_and_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite'
            with TelemetryStore(path, source='synthetic', config={}) as store:
                first = store.session_id
                record = dict(link_id='s2-s5', observation_timestamp='2026-10-15T00:00:00Z',
                    monotonic_elapsed_time=5, utilization=None, status='initial', valid=False)
                store.write_batch([], [], [record], [])
            with TelemetryStore(path, source='synthetic', config={}) as store:
                self.assertNotEqual(store.session_id, first)
            rows = read_rows(path, 'observations')
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]['utilization'])
            self.assertEqual(rows[0]['source'], 'synthetic')
            with self.assertRaises(ValueError):
                read_rows(path, 'invalid_table')

    def test_partial_batch_and_invalid_order_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite'
            with TelemetryStore(path, 'synthetic', {}) as store:
                row = dict(link_id='s2-s5', observation_timestamp='2026-10-15T00:00:05Z',
                           monotonic_elapsed_time=5, valid=False, utilization=None, status='initial')
                store.write_batch([], [], [row], [])
                with self.assertRaises(ValueError):
                    store.write_batch([], [], [dict(row, monotonic_elapsed_time=4)], [])
                with self.assertRaises((KeyError, ValueError)):
                    store.write_batch([], [], [dict(row, monotonic_elapsed_time=10), {}], [])
            self.assertEqual(len(read_rows(path, 'observations')), 1)


if __name__ == '__main__':
    unittest.main()
