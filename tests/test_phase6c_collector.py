"""Exercise the real Phase 6C collector and Phase 5 lifecycle without a network."""
import importlib.util
from itertools import product
import json
from datetime import datetime,timezone
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import collect_phase6c as collector
from phase6c import make_four_switch_plan,make_six_switch_plan,validate_collection


@unittest.skipUnless(importlib.util.find_spec('mininet'),'Ubuntu Mininet Python required')
class CollectorPlanRegressionTests(unittest.TestCase):
    def test_wall_clock_jump_does_not_truncate_probes_or_the_recorded_plan_in_either_mode(self):
        import integration_phase5 as live
        from test_phase5_lifecycle import Clock,Process
        for mode,probe_lag,preset in product(('baseline','predictive_ml'),(.5,10),('phase6c','six-switch')):
            with self.subTest(mode=mode,probe_lag=probe_lag,preset=preset),tempfile.TemporaryDirectory() as directory:
                output=Path(directory);plan=make_six_switch_plan() if preset=='six-switch' else make_four_switch_plan(profile='feasibility');clock=Clock()
                native=preset=='six-switch';wiring=((1,4,[1,2,5,4]),(2,5,[2,5]),(3,6,[3,2,5,6])) if native else ((1,2,[1,2,4]),(3,4,[2,4]))
                def wall(at=None):
                    at=clock.now if at is None else at
                    return 1700000000+at+(3 if at>=70 else 0)
                timer=SimpleNamespace(time=wall,monotonic=clock.monotonic,sleep=clock.sleep)
                processes=[]
                class Host:
                    def __init__(self,name):self.name=name
                    def IP(self):return '10.0.0.'+self.name[1:]
                    def popen(self,command,**kwargs):
                        finish=None
                        if command[0]=='ping':
                            count=int(command[command.index('-c')+1]);interval=float(command[command.index('-i')+1])
                            kwargs['stdout'].write(''.join(
                                f'[{wall(clock.now+i*interval):.6f}] 64 bytes: icmp_seq={i+1} time=8.0 ms\n'
                                for i in range(count))+f'{count} packets transmitted, {count} received,\n')
                            finish=clock.now+count*interval+probe_lag
                        elif '-c' in command:
                            seconds=int(command[command.index('-t')+1]);rate=float(command[command.index('-b')+1][:-1])
                            kwargs['stdout'].write(json.dumps(dict(
                                start=dict(test_start=dict(duration=seconds,protocol='UDP',target_bitrate=rate*1e6),
                                    connected=[dict(local_host=self.IP(),remote_host=command[command.index('-c')+1])],
                                    version='fixture',system_info='fixture'),
                                end=dict(sum_sent=dict(seconds=seconds,bytes=1000*seconds),
                                    sum_received=dict(seconds=seconds,bytes=1000*seconds,packets=seconds,lost_packets=0)))))
                            finish=clock.now+seconds
                        process=Process(clock,command,finish);processes.append(process);return process
                class Network(dict):
                    switches=[SimpleNamespace(name=f's{i}') for i in (range(1,7) if native else range(1,5))]
                    def pingAll(self,**kwargs):return 0
                net=Network({f'h{i}':Host(f'h{i}') for i in (range(1,7) if native else range(1,5))})
                def state(*args,**kwargs):
                    routes=[]
                    for src,dst,path in wiring:
                        routes.append(dict(source_mac=f'00:00:00:00:00:{src:02x}',destination_mac=f'00:00:00:00:00:{dst:02x}',
                            baseline_path=path,installed_route=dict(path=path,cookie=src),observed_route=dict(cookie=src,
                                counters=[dict(dpid=d,source_mac=f'00:00:00:00:00:{h:02x}',packet_count=1+int(clock.now*100))
                                    for h in (src,dst) for d in path])))
                    return dict(stale=False,snapshot=dict(routes=routes))
                args=SimpleNamespace(output=output,database=output/'state.sqlite',routing_mode=mode,trial_id='fixture')
                config=dict(routing_mode=mode,trial_id=args.trial_id,traffic_configuration=plan,
                    experiment_link_capacities_mbps=plan['link_capacities_mbps'])
                sessions=[dict(session_id='fixture-session',source='live_mininet',config_json=json.dumps(config))]
                result=dict(stages=[],scoped_cleanup_verified=True)
                with patch.object(collector,'time',timer),patch.object(live,'time',timer), \
                     patch.object(collector,'utc',side_effect=lambda:datetime.fromtimestamp(wall(),timezone.utc).isoformat()), \
                     patch.object(collector,'capture_tc',return_value=[]), \
                     patch('mininet_topology.command',return_value='fixture OVS/TC'), \
                     patch('phase3.sessions',return_value=sessions),patch('routing_state.read_routing_state',side_effect=state):
                    if probe_lag>3:
                        with self.assertRaisesRegex(RuntimeError,'Incomplete RTT measurement'):
                            collector.collect(net,args,result,plan)
                    else:collector.collect(net,args,result,plan)
                traffic=json.loads((output/'traffic/traffic.json').read_text())
                if probe_lag>3:
                    self.assertLess(len(traffic['stages']),len(plan['stages']))
                    self.assertTrue(any(not p['complete'] for p in traffic['stages'][-1]['probes']))
                    self.assertTrue(all(p.returncode is not None for p in processes))
                    continue
                fields=('scenario','rate_mbps','seconds','reverse','planned_elapsed_seconds')
                self.assertEqual([{k:s[k] for k in fields} for s in traffic['stages']],plan['stages'])
                self.assertEqual(len(traffic['stages']),4 if native else 9)  # the 20s tail is not a traffic stage
                if native:
                    self.assertEqual([c['rate_mbps'] for c in traffic['stages'][2]['clients']],[60,80,80])
                    self.assertEqual([f['rate_mbps'] for f in traffic['stages'][2]['flows']],[60,80,80])
                self.assertEqual(traffic['seed'],plan['seed'])
                self.assertEqual(traffic['requested_traffic_seconds'],plan['seconds'])
                self.assertEqual(validate_collection(plan,result),[])
                self.assertGreaterEqual(clock.now-traffic['elapsed_seconds'],plan['telemetry_tail_seconds'])
                self.assertTrue(all(p.returncode==0 or p.args[0]=='iperf3' and '-s' in p.args for p in processes))
