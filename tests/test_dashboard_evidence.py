"""Dashboard adapters use temporary evidence; never launch a network."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_phase6c_analysis import fixture,stamp,a
from phase6c import make_six_switch_plan,write_json,digest


class DashboardEvidenceTests(unittest.TestCase):
    def adapter(self):
        import dashboard_evidence
        return dashboard_evidence

    def test_abba_is_revalidated_and_aggregated_without_touching_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'experiment';root.mkdir();plan=make_six_switch_plan()
            write_json(root/'experiment.json',dict(order=['baseline','predictive_ml','predictive_ml','baseline'],
                trial_count=4,plan_hash=digest(plan),model_sha256='fixture-only-model-hash',source_sha256={'fixture':'not-live'}))
            for i in range(1,5):fixture(root,i,plan)
            before=a.b.inventory(root);package=self.adapter().build_package(root)
            self.assertEqual(a.b.inventory(root),before)
            self.assertEqual(package['status'],'available')
            self.assertIsInstance(package['provenance']['validator_sha256'],str)
            self.assertEqual(package['comparison']['average_ms']['baseline'],20)
            self.assertEqual(package['comparison']['average_ms']['change_percent'],0)
            self.assertAlmostEqual(package['comparison']['wall_aggregate_goodput_mbps']['baseline'],.024)
            self.assertIsNone(package['comparison']['icmp_loss_percent']['change_percent'])
            self.assertEqual(package['trials'][1]['mode'],'predictive_ml')
            self.assertEqual(package['trials'][0]['snapshots'][0]['routes'][0]['installed_route']['cookie_hex'],'0x1')
            status=root/'02-predictive_ml/trial-status.json';value=json.loads(status.read_text());value['status']='failed';write_json(status,value)
            with self.assertRaisesRegex(ValueError,'passed'):self.adapter().build_package(root)

    def test_missing_or_corrupt_portable_data_never_becomes_fixture_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'absent.json'
            result=self.adapter().load_package(path)
            self.assertEqual(result['status'],'unavailable');self.assertFalse(path.exists())
            path.write_text('{}');self.assertEqual(self.adapter().load_package(path)['status'],'unavailable')

    def test_snapshot_link_measurements_and_prediction_timestamps_are_preserved(self):
        snapshot=dict(timestamp=stamp(5),topology_epoch=12,routes=[],topology=dict(connected_switches=[1,2,3,4,5,6],
            links=[dict(link_id='s2-s5',source_dpid=2,destination_dpid=5,active=True,capacity_mbps=200,base_delay_ms=2,
                measurement=dict(valid=True,utilization=.9,observation_timestamp=stamp(4)),
                prediction=dict(status='valid',predicted_utilization=.95,target_timestamp=stamp(19)))]))
        link=self.adapter().snapshot_view(snapshot)['topology']['links'][0]
        self.assertEqual(link['current_utilization'],.9)
        self.assertEqual(link['target_timestamp'],stamp(19))
        self.assertEqual(link['observation_timestamp'],stamp(4))

    def test_recovery_requires_a_recorded_down_and_restore_sequence(self):
        from topology_config import wired_links
        def snapshot(t,inactive=False):
            return dict(timestamp=stamp(t),topology_epoch=1,policy={},routes=[],
                topology=dict(connected_switches=[1,2,3,4,5,6],switches=[],hosts=[],links=[dict(
                    link_id=f's{min(l["src"],l["dst"])}-s{max(l["src"],l["dst"])}',source_dpid=l['src'],destination_dpid=l['dst'],
                    active=not(inactive and {l['src'],l['dst']}=={2,5}),capacity_mbps=100,base_delay_ms=l['base_delay']) for l in wired_links('six')]))
        snaps=[snapshot(0),snapshot(5,True),snapshot(10),snapshot(10.1),snapshot(12)]
        result=self.adapter().recovery_window(snaps,[])
        self.assertEqual(result['status'],'available')
        self.assertEqual(result['failed_links'],['s2-s5'])
        self.assertFalse(next(l for l in result['snapshots'][1]['topology']['links'] if l['id']=='s2-s5')['active'])
        self.assertTrue(next(l for l in result['snapshots'][-1]['topology']['links'] if l['id']=='s2-s5')['active'])
        self.assertEqual(result['snapshots'][-1]['timestamp'],stamp(12))
        self.assertEqual(self.adapter().recovery_window(snaps[:2],[])['status'],'unavailable')

    def test_cookie_evidence_requires_matching_version_and_every_unique_hop(self):
        route=dict(source_mac='00:00:00:00:00:03',destination_mac='00:00:00:00:00:06',
            installed_route=dict(cookie=0x4652000000000041,path=[3,2,5,6]),
            observed_route=dict(cookie=0x4652000000000041,counters=[dict(dpid=i,source_mac='00:00:00:00:00:03',packet_count=9) for i in [3,2,5,6]]))
        self.assertTrue(self.adapter().route_proof(route,3))
        broken=copy.deepcopy(route);broken['observed_route']['counters'][-1]['dpid']=3
        self.assertFalse(self.adapter().route_proof(broken,3))
        route['observed_route']['cookie']+=1;self.assertFalse(self.adapter().route_proof(route,3))

    def test_missing_replay_does_not_prevent_benchmark_or_live_dashboard_startup(self):
        from demo_data import DemoData
        with tempfile.TemporaryDirectory() as directory:
            absent=Path(directory)/'missing.sqlite'
            demo=DemoData(absent,absent)
            self.assertEqual(demo.snapshot()['health']['telemetry'],'unavailable')
            self.assertIsNotNone(demo.snapshot()['health']['error'])
            self.assertFalse(absent.exists())

    def test_read_only_benchmark_api_and_live_replay_controls(self):
        from http.server import ThreadingHTTPServer
        from urllib.request import urlopen,Request
        import threading
        from demo_server import handler_for
        from demo_data import DemoData
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);demo=DemoData(root/'missing.sqlite',root/'missing-live.sqlite')
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(demo,root,dict(status='unavailable',reason='No test data')))
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                self.assertEqual(json.load(urlopen(base+'/api/benchmark'))['status'],'unavailable')
                request=Request(base+'/api/control',data=json.dumps(dict(action='mode',mode='live')).encode(),headers={'Content-Type':'application/json'})
                state=json.load(urlopen(request));self.assertEqual(state['mode'],'live')
                self.assertEqual(json.load(urlopen(base+'/api/routing'))['status'],'unavailable')
                self.assertFalse(list(root.glob('*.sqlite')))
            finally:server.shutdown();server.server_close();worker.join()


if __name__=='__main__':unittest.main()
