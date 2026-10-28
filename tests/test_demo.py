"""Focused demo checks; no Mininet, retraining or traffic experiments."""
import tempfile
import unittest
from pathlib import Path

from demo_data import DemoData, calculate_routes

RECORDING = Path(__file__).resolve().parents[1]/'data/demo-replay.sqlite'


class DemoTests(unittest.TestCase):
    @unittest.skipUnless(RECORDING.exists(), 'Validated local recording is not present')
    def test_replay_real_records_next_event_and_reset(self):
        demo = DemoData(RECORDING, Path('/tmp/no-flowrider-live.sqlite'))
        initial = demo.snapshot()
        self.assertEqual(len(initial['topology']['nodes']), 6)
        self.assertEqual(len(initial['topology']['links']), 7)
        self.assertEqual(initial['mode'], 'replay')
        demo.control({'action': 'next_event'})
        state = demo.snapshot()
        self.assertGreater(state['playback']['position'], initial['playback']['position'])
        self.assertTrue(any(l['congestion_predicted'] for l in state['topology']['links']))
        self.assertFalse(state['routes']['forwarding_verified'])
        demo.control({'action': 'route', 'source': 3, 'destination': 6})
        demo.control({'action': 'reset'})
        self.assertAlmostEqual(demo.snapshot()['playback']['position'], 0, places=1)
        self.assertEqual(demo.snapshot()['routes']['source'], 1)

    def test_weight_equation_and_route_preview(self):
        from topology_state import TopologyState
        graph = TopologyState().graph
        links = []
        for u, v, edge in graph.edges(data=True):
            links.append(dict(id=f's{min(u,v)}-s{max(u,v)}', source=u, target=v,
                active=True, predicted_utilization=.95 if {u,v} == {2,5} else .05,
                current_utilization=.1, capacity_mbps=edge['capacity']*.1))
        routes = calculate_routes(links, 1, 4)
        self.assertEqual(routes['baseline']['path'], ['s1', 's2', 's5', 's4'])
        self.assertNotIn('s2-s5', routes['proposed']['links'])
        self.assertAlmostEqual(next(w['weight'] for w in routes['weights'] if w['link_id']=='s2-s5'), 2*(1+25*.95**4))
        links[0]['predicted_utilization'] = None
        self.assertIsNone(calculate_routes(links, 1, 4)['proposed'])

    @unittest.skipUnless(RECORDING.exists(), 'Validated local recording is not present')
    def test_live_missing_database_is_unknown_without_replay_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            demo = DemoData(RECORDING, Path(directory)/'absent.sqlite', mode='live')
            state = demo.snapshot()
            self.assertEqual(state['mode'], 'live')
            self.assertEqual(state['health']['telemetry'], 'unavailable')
            self.assertTrue(all(l['current_utilization'] is None for l in state['topology']['links']))
            self.assertIsNone(state['routes']['proposed'])
            self.assertFalse((Path(directory)/'absent.sqlite').exists())
        stale = DemoData(RECORDING, RECORDING, mode='live').snapshot()
        self.assertEqual(stale['health']['telemetry'], 'stale')
        self.assertTrue(all(l['current_utilization'] is None for l in stale['topology']['links']))


if __name__ == '__main__':
    unittest.main()
