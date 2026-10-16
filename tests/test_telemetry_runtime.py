import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from telemetry_runtime import PollCollector, TelemetryWorker
from telemetry_store import read_rows
from telemetry import link_snapshot
import test_controller as phase2
from test_controller import Datapath, event
from test_telemetry import counters
from test_topology_state import activate
from topology_state import TopologyState


class Sink:
    def __init__(self):
        self.items = []
    def submit(self, kind, value):
        self.items.append((kind, value))
        return True


class PollTests(unittest.TestCase):
    def setUp(self):
        self.state = TopologyState()
        activate(self.state)
        self.dps = {i: Datapath(i) for i in self.state.switches}
        self.sink = Sink()
        self.clock = 100.0
        self.poller = PollCollector(self.sink, clock=lambda: self.clock)

    def reply(self, dp, more=False, xid=None):
        xid = xid if xid is not None else self.poller.pending[dp.id]['xid']
        body = [NS(port_no=p, duration_sec=100, duration_nsec=0, **counters())
                for p in self.state.expected_ports(dp.id)]
        return self.poller.reply(event(dp, xid=xid, flags=1 if more else 0, body=body).msg)

    def test_poll_requests_all_valid_switches_and_correlates_final_replies(self):
        self.poller.poll(self.state, self.dps)
        for dp in self.dps.values():
            self.assertEqual(dp.messages[-1].port_no, dp.ofproto.OFPP_ANY)
            self.reply(dp)
        batches = [v for kind, v in self.sink.items if kind == 'sample']
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]['samples']), 14 + 6)
        self.assertFalse(self.reply(self.dps[1], xid=999))

    def test_stale_delayed_duplicates_missing_and_disconnect(self):
        self.poller.poll(self.state, self.dps)
        self.assertFalse(self.reply(self.dps[1], xid=999))
        self.clock += 4.5
        self.assertFalse(self.reply(self.dps[1]))
        self.clock += 0.5
        self.poller.poll(self.state, self.dps)
        batch = [v for kind, v in self.sink.items if kind == 'sample'][0]
        self.assertIn('missing', batch['failures'].values())
        self.state.unregister_switch(2)
        self.dps.pop(2)
        self.poller.topology_changed(self.state, self.dps)
        self.assertFalse(self.poller.pending)

    def test_multipart_does_not_publish_incomplete_or_duplicate_ports(self):
        self.poller.poll(self.state, {1: self.dps[1]})
        self.reply(self.dps[1], more=True)
        self.assertFalse(any(kind == 'sample' for kind, _ in self.sink.items))
        self.reply(self.dps[1])
        batch = [v for kind, v in self.sink.items if kind == 'sample'][0]
        self.assertEqual(batch['failures'][1], 'duplicate_ports')

    def test_statistics_errors_remain_separate_from_forwarding_errors(self):
        self.poller.poll(self.state, self.dps)
        xid = self.poller.pending[1]['xid']
        self.assertTrue(self.poller.statistics_error(self.dps[1], xid))
        self.assertFalse(self.dps[1].closed)
        self.assertFalse(self.poller.statistics_error(self.dps[1], 99999))


class WorkerTests(unittest.TestCase):
    def test_storage_and_model_not_ready_with_real_worker_thread(self):
        state = TopologyState()
        activate(state)
        links, ports = link_snapshot(state, {i: object() for i in state.switches})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.sqlite'
            worker = TelemetryWorker(path, source='simulated_openflow', logger=logging.getLogger('test'), clock=lambda: 120)
            for index in range(5):
                samples = {key: dict(counters=counters(index * 10_000_000), duration=100 + 5 * index,
                    monotonic_time=100 + 5 * index, observation_timestamp=f'2026-10-15T00:00:{index * 5:02d}Z')
                    for key in ports}
                worker.submit('sample', dict(poll_id=index, links=links, ports=ports, samples=samples,
                    failures={}, monotonic_time=100 + 5 * index,
                    observation_timestamp=f'2026-10-15T00:00:{index * 5:02d}Z'))
            worker.close()
            rows = read_rows(path, 'observations')
            self.assertEqual(len(rows), 35)
            self.assertEqual(rows[-1]['source'], 'simulated_openflow')
            self.assertIsNotNone(rows[-1]['utilization'])
            self.assertEqual(read_rows(path, 'predictions')[-1]['status'], 'model_not_ready')

    def test_full_synthetic_counter_to_saved_model_inference_pipeline(self):
        from test_prediction import observations
        from traffic_engine import train_live_predictor
        state = TopologyState()
        activate(state)
        links, ports = link_snapshot(state, {i: object() for i in state.switches})
        with tempfile.TemporaryDirectory() as directory:
            model, db = Path(directory) / 'model.pkl', Path(directory) / 'data.sqlite'
            train_live_predictor(observations(), model, source='synthetic')
            worker = TelemetryWorker(db, source='synthetic', model_path=model, clock=lambda: 135)
            for index in range(8):
                wall = f'2026-10-16T00:00:{index * 5:02d}Z'
                samples = {key: dict(counters=counters(index * 50_000_000), duration=index * 5,
                    monotonic_time=100 + index * 5, observation_timestamp=wall) for key in ports}
                worker.submit('sample', dict(poll_id=index, links=links, ports=ports, samples=samples,
                    failures={}, monotonic_time=100 + index * 5, observation_timestamp=wall))
            worker.close()
            self.assertIsNone(worker.failure)
            predictions = [p for p in read_rows(db, 'predictions') if p['status'] == 'valid']
            self.assertTrue(predictions)
            self.assertTrue(all(p['source'] == 'synthetic' for p in predictions))
            for p in predictions:
                self.assertEqual(p['congestion_predicted'], p['predicted_utilization'] >= 0.8)

    def test_bad_model_and_storage_failure_do_not_escape_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'corrupt.pkl'
            path.write_bytes(b'not a pickle')
            state = TopologyState()
            links, ports = link_snapshot(state, {})
            logger = logging.getLogger('bad-model-test')
            with self.assertLogs(logger, level='ERROR'):
                worker = TelemetryWorker(Path(directory) / 'bad.sqlite', model_path=path, logger=logger)
                worker.submit('topology', dict(links=links, ports=ports, monotonic_time=100,
                                             observation_timestamp='2026-10-15T00:00:00Z'))
                worker.close()
            self.assertIsNone(worker.failure)

    def test_unwritable_database_disables_worker_without_raising_to_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = logging.getLogger('failed-storage-test')
            with self.assertLogs(logger, level='ERROR'):
                worker = TelemetryWorker(Path(directory), logger=logger)
                worker.close()
            self.assertIsNotNone(worker.failure)
            self.assertFalse(worker.submit('sample', {}))


class ForwardingIsolationTests(phase2.ControllerTests):
    # Reuse all Phase 2 behavior checks with a deliberately broken telemetry hook.
    def setUp(self):
        super().setUp()
        quiet = patch.object(self.app.logger, 'exception')
        quiet.start()
        self.addCleanup(quiet.stop)
        self.broken = NS(topology_changed=lambda *args: (_ for _ in ()).throw(RuntimeError('disk failed')))
        self.app.telemetry = self.broken


if __name__ == '__main__':
    unittest.main()
