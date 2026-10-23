"""Controller/worker/storage seams for automatic routing (no privileged network)."""
import json
import logging
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS

from os_ken.ofproto import ofproto_v1_3_parser as parser
from controller import EventRoutingTelemetry, EventRoutingTick
from predictive_routing import RoutingManager
from telemetry_runtime import TelemetryWorker
from telemetry_store import TelemetryStore, read_rows
from routing_state import read_routing_state
import test_controller as phase2
from test_controller import event


class ControllerRoutingTests(phase2.ControllerTests):
    # The inherited cases intentionally run unchanged with routing disabled.
    def enable(self):
        self.app.routing=RoutingManager(self.app.topology,self.app.datapaths,
            release=self.app._release_routing_packet)

    def test_opt_in_packet_stages_whole_path_and_waits_before_packetout(self):
        self.ready();self.enable();self.packet_in()
        self.assertEqual(len(self.app.routing.pairs),1)
        self.assertFalse(any(isinstance(m,parser.OFPPacketOut) for dp in self.dps.values() for m in dp.messages))
        self.assertTrue(any(isinstance(m,parser.OFPFlowMod) and m.priority==20 for dp in self.dps.values() for m in dp.messages))
        self.assertFalse(any(isinstance(m,parser.OFPFlowMod) and m.priority==10 for dp in self.dps.values() for m in dp.messages))
        self.app.routing_tick_handler(EventRoutingTick())

    def test_unknown_unicast_broadcast_behavior_is_preserved_when_enabled(self):
        self.ready();self.enable();self.packet_in(dst='ff:ff:ff:ff:ff:ff')
        self.assertFalse(self.app.routing.pairs)
        self.assertIsInstance(self.dps[1].messages[-1],parser.OFPPacketOut)

    def test_telemetry_event_is_serialized_and_old_epoch_is_ignored(self):
        self.ready();self.enable()
        self.app.routing_telemetry_handler(EventRoutingTelemetry(dict(epoch=-1,source='live_mininet')))
        self.assertIsNone(self.app.routing.telemetry_time)
        self.app._invalidate_routes()
        self.assertEqual(self.app.routing.epoch,1)

    def test_truncated_known_packet_is_not_staged(self):
        self.ready();self.enable()
        self.app.packet_in_handler(event(self.dps[1],match={'in_port':1},
            data=phase2.frame(),buffer_id=self.dps[1].ofproto.OFP_NO_BUFFER,msg_len=120,total_len=80))
        self.assertFalse(self.app.routing.pairs)

    def test_release_reinjects_full_unbuffered_packet_through_table(self):
        self.ready();self.enable()
        self.app._release_routing_packet(dict(dpid=1,datapath=self.dps[1],ingress=1,data=b'frame'))
        out=self.dps[1].messages[-1]
        self.assertEqual(out.actions[0].port,self.dps[1].ofproto.OFPP_TABLE)
        self.assertEqual(out.buffer_id,self.dps[1].ofproto.OFP_NO_BUFFER)


class RoutingPersistenceTests(unittest.TestCase):
    def test_additive_routing_tables_and_read_only_snapshot_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.sqlite'
            with TelemetryStore(path,'live_mininet',{}) as store:
                store.write_routing('event',dict(kind='route_activated',timestamp='2026-10-23T00:00:00Z',route_id='one'))
                store.write_routing('snapshot',dict(timestamp='2026-10-23T00:00:01Z',routes=[],source='live_mininet'))
                session=store.session_id
            state=read_routing_state(path)
            self.assertEqual(state['session_id'],session)
            self.assertEqual(state['events'][0]['kind'],'route_activated')
            self.assertTrue(state['stale'])
            self.assertEqual(read_rows(path,'observations'),[])
            missing=Path(directory)/'missing.sqlite'
            self.assertEqual(read_routing_state(missing)['status'],'unavailable')
            self.assertFalse(missing.exists())

    def test_real_worker_persists_routes_without_entering_telemetry_frame_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.sqlite'
            worker=TelemetryWorker(path,source='live_mininet',logger=logging.getLogger('routing-worker'))
            worker.submit('routing',dict(kind='event',record=dict(kind='candidate',timestamp='2026-10-23T00:00:00Z')))
            worker.submit('routing',dict(kind='snapshot',record=dict(timestamp='2026-10-23T00:00:00Z',routes=[])))
            worker.close()
            self.assertIsNone(worker.failure)
            self.assertEqual(read_routing_state(path)['events'][0]['kind'],'candidate')

class RoutingApiTests(unittest.TestCase):
    def test_read_only_routing_endpoint_uses_controller_database(self):
        from http.server import ThreadingHTTPServer
        import threading
        from urllib.request import urlopen
        from demo_server import handler_for
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'routes.sqlite'
            with TelemetryStore(path,'live_mininet',{}) as store:
                from telemetry_store import utc_now
                store.write_routing('snapshot',dict(timestamp=utc_now(),routes=[dict(
                    installed_route=dict(cookie=0x465200000000007b),
                    observed_route=dict(cookie=0x465200000000007c))],automatic_rerouting=True))
            demo=NS(live_db=path,mode='replay',snapshot=lambda:{})
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(demo,Path(directory)))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with urlopen(f'http://127.0.0.1:{server.server_port}/api/routing') as reply:
                    body=json.load(reply)
                self.assertEqual(body['source'],'live_mininet')
                self.assertEqual(body['view_mode'],'replay')
                self.assertTrue(body['snapshot']['automatic_rerouting'])
                self.assertEqual(body['status'],'available')
                route=body['snapshot']['routes'][0]
                self.assertEqual(route['installed_route']['cookie_hex'],'0x465200000000007b')
                self.assertEqual(route['observed_route']['cookie_hex'],'0x465200000000007c')
            finally:server.shutdown();server.server_close();thread.join(2)

class WorkerRoutingCallbackTests(unittest.TestCase):
    def test_committed_counter_batch_notifies_controller_with_topology_epoch(self):
        from telemetry import link_snapshot
        from topology_state import TopologyState
        from test_topology_state import activate
        from test_telemetry import counters
        state=TopologyState();activate(state)
        links,ports=link_snapshot(state,{i:object() for i in state.switches});batches=[]
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'callback.sqlite'
            worker=TelemetryWorker(db,source='simulated_openflow',clock=lambda:105,on_batch=batches.append)
            for i in range(2):
                now=100+5*i;wall=f'2026-10-23T00:00:{5*i:02d}Z'
                samples={key:dict(counters=counters(i*10_000_000),duration=now,
                    monotonic_time=now,observation_timestamp=wall) for key in ports}
                worker.submit('sample',dict(poll_id=i,links=links,ports=ports,samples=samples,failures={},
                    monotonic_time=now,observation_timestamp=wall,routing_epoch=23))
            worker.close()
            self.assertIsNone(worker.failure);self.assertEqual(len(batches),2)
            self.assertEqual(batches[-1]['epoch'],23)
            self.assertEqual(batches[-1]['source'],'simulated_openflow')
            self.assertTrue(all(o['valid'] for o in batches[-1]['observations']))
            self.assertEqual(len(read_rows(db,'predictions')),14)
