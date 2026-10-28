"""Real OS-Ken TCP and simulated OF1.3 switches; NOT live Mininet."""
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from routing_state import read_routing_state
from telemetry_store import read_rows
from topology_config import HOSTS,TOPOLOGY_VERSION
from test_predictive_socket import Fabric,RoutingPeer


class BaselineSocketTests(unittest.TestCase):
    def test_explicit_baseline_installs_bidirectionally_collects_and_recovers(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile(mode='w+') as log:
            root=Path(directory);db=root/'baseline.sqlite';traffic=root/'traffic.json'
            traffic.write_text(json.dumps(dict(seed=42,flows=[['h1','h4']],seconds=60)))
            fabric=Fabric();peers=[]
            process=subprocess.Popen([sys.executable,'-u','controller.py','--ofp-listen-host','127.0.0.1',
                '--ofp-tcp-listen-port',str(port),'--telemetry-enabled','--telemetry-source','simulated_openflow',
                '--telemetry-db',str(db),'--routing-mode','baseline','--trial-id','baseline-wire',
                '--traffic-config',str(traffic)],stdout=log,stderr=subprocess.STDOUT)
            def wait(check,seconds=10):
                deadline=time.monotonic()+seconds
                while time.monotonic()<deadline:
                    result=check()
                    if result:return result
                    if process.poll() is not None:break
                    time.sleep(.05)
                log.seek(0);self.fail('Baseline verification timed out\n'+log.read())
            def route():
                state=read_routing_state(db)
                return next((r for r in (state.get('snapshot') or {}).get('routes',[]) if
                    r['source_mac']==HOSTS[1]['mac'] and r['destination_mac']==HOSTS[4]['mac'] and r['installed_route']),None)
            try:
                deadline=time.monotonic()+7
                while True:
                    try:peers=[RoutingPeer(1,port,fabric)];break
                    except ConnectionRefusedError:
                        if time.monotonic()>deadline:raise
                        time.sleep(.05)
                peers += [RoutingPeer(i,port,fabric) for i in range(2,7)]
                self.assertTrue(all(p.ready.wait(5) for p in peers));time.sleep(.2)
                peers[0].inject(1,4)
                initial=wait(route);cookie=initial['installed_route']['cookie']
                self.assertEqual(initial['installed_route']['path'],[1,2,5,4])
                self.assertEqual(fabric.forward(1,HOSTS[1]['mac'],HOSTS[4]['mac']),[1,2,5,4])
                self.assertEqual(fabric.forward(4,HOSTS[4]['mac'],HOSTS[1]['mac']),[4,5,2,1])
                def collected():
                    rows=read_rows(db,'observations')
                    return any(r.get('valid') for r in rows)
                wait(collected,seconds=16)
                self.assertEqual(route()['installed_route']['cookie'],cookie)
                snapshot=read_routing_state(db)['snapshot']
                self.assertEqual(snapshot['routing_mode'],'baseline')
                self.assertFalse(snapshot['automatic_rerouting'])
                self.assertTrue(all(l['routing_weight']==l['base_delay_ms'] for l in snapshot['topology']['links']))
                with sqlite3.connect(db) as connection:
                    config=json.loads(connection.execute('SELECT config_json FROM sessions').fetchone()[0])
                    events=[json.loads(s) for s, in connection.execute('SELECT record_json FROM routing_events')]
                self.assertEqual(config['routing_mode'],'baseline')
                self.assertEqual(config['trial_id'],'baseline-wire')
                self.assertEqual(config['topology_version'],TOPOLOGY_VERSION)
                self.assertEqual(config['traffic_configuration']['seed'],42)
                self.assertFalse(any(e['kind']=='reroute_candidate' or e.get('reason')=='predicted_congestion' for e in events))
                peers[1].change_port(4,up=False)
                wait(lambda:not route());peers[0].inject(1,4)
                changed=wait(route)
                self.assertNotEqual(changed['installed_route']['path'],[1,2,5,4])
                self.assertEqual(fabric.forward(1,HOSTS[1]['mac'],HOSTS[4]['mac']),changed['installed_route']['path'])
                peers[1].change_port(4,up=True)
                wait(lambda:not route());peers[0].inject(1,4)
                restored=wait(route)
                self.assertEqual(restored['installed_route']['path'],[1,2,5,4])
                self.assertEqual(fabric.forward(4,HOSTS[4]['mac'],HOSTS[1]['mac']),[4,5,2,1])
                self.assertFalse(fabric.errors)
                self.assertTrue(all(not p.errors for p in peers))
            finally:
                for peer in peers:peer.close()
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                log.seek(0);output=log.read()
                self.assertNotIn('Traceback',output,output)
                self.assertNotIn('ERROR:',output,output)
