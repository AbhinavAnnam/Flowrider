"""Phase 6C measurement contracts; fixtures are not live measurements."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest

from phase6c import (make_plan, validate_plan, create_trial, parse_ping,
                    rtt_metrics, forwarding_proof, validate_collection, check_queue)


PING = '''[100.010] 64 bytes from 10.0.0.4: icmp_seq=1 ttl=64 time=10.0 ms
[100.220] 64 bytes from 10.0.0.4: icmp_seq=2 ttl=64 time=20.0 ms
3 packets transmitted, 2 received, 33.3333% packet loss, time 400ms
'''


class Phase6CTests(unittest.TestCase):
    def test_seeded_plan_has_shared_overload_and_spare_ring_capacity(self):
        p = make_plan()
        self.assertEqual(p, make_plan())
        self.assertEqual(p['bottleneck']['link_id'], 's2-s5')
        self.assertEqual(p['link_capacities_mbps'], {'s2-s5': 50})
        self.assertGreater(2 * next(s['rate_mbps'] for s in p['stages'] if s['scenario']=='sustained'), 50)
        self.assertLess(2 * max(s['rate_mbps'] for s in p['stages']), 100)
        self.assertEqual({s['scenario'] for s in p['stages']}, {'low','buildup','sustained','burst','recovery'})

    def test_invalid_bottleneck_and_probe_config_rejected(self):
        for field,value in [('bandwidth_mbps',0),('bandwidth_mbps',float('nan')),
                            ('bandwidth_mbps',10),('bandwidth_mbps',200),('delay_ms',3),('queue_packets',0)]:
            p=make_plan();p['bottleneck'][field]=value
            if field=='bandwidth_mbps':p['link_capacities_mbps']['s2-s5']=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):validate_plan(p)
        p=make_plan();p['probe_interval_seconds']=0
        with self.assertRaises(ValueError):validate_plan(p)

    def test_trial_order_isolated_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'experiment';root.mkdir()
            trials=[create_trial(root,i,make_plan(),'model-hash') for i in range(1,5)]
            self.assertEqual([t['routing_mode'] for t in trials],['baseline','predictive_ml','predictive_ml','baseline'])
            self.assertEqual(len({t['trial_id'] for t in trials}),4)
            self.assertEqual(len({t['database'] for t in trials}),4)
            with self.assertRaises(FileExistsError):create_trial(root,1,make_plan(),'model-hash')
            with self.assertRaises(ValueError):create_trial(root,5,make_plan(),'model-hash')

    def test_ping_uses_timestamped_rtt_and_summary_loss(self):
        result=parse_ping(PING,3)
        self.assertEqual(result['received'],2)
        self.assertAlmostEqual(result['loss_percent'],100/3)
        metrics=rtt_metrics(result['samples'])
        self.assertEqual(metrics['sample_count'],2)
        self.assertEqual(metrics['average_ms'],15)
        self.assertEqual(metrics['median_ms'],15)
        self.assertEqual(metrics['p95_ms'],19.5)
        self.assertEqual(metrics['maximum_ms'],20)
        self.assertEqual(result['samples'][0]['timestamp'],100.010)

    def test_missing_summary_or_samples_never_becomes_zero_latency(self):
        for text in ('',PING.replace('2 received','3 received'),PING.replace('[100.010]','')):
            with self.assertRaises(ValueError):parse_ping(text,3)
        lost=parse_ping('3 packets transmitted, 0 received, 100% packet loss, time 400ms',3)
        self.assertEqual(lost['loss_percent'],100)
        self.assertIsNone(rtt_metrics(lost['samples'])['p95_ms'])

    def test_routing_proof_requires_actual_cookie_and_all_positive_hops(self):
        r=dict(installed_route=dict(cookie=7,path=[1,2,5,4]),observed_route=dict(cookie=7,
            counters=[dict(dpid=i,source_mac='00:00:00:00:00:01',packet_count=1) for i in (1,2,5,4)]))
        self.assertTrue(forwarding_proof(r,1))
        broken=copy.deepcopy(r);broken['observed_route']['cookie']=6
        self.assertFalse(forwarding_proof(broken,1))
        broken=copy.deepcopy(r);broken['observed_route']['counters'].pop()
        self.assertFalse(forwarding_proof(broken,1))
        broken=copy.deepcopy(r);broken['observed_route']['counters'][0]['packet_count']=0
        self.assertFalse(forwarding_proof(broken,1))

    def test_partial_stage_or_missing_probe_is_failure(self):
        p=make_plan()
        errors=validate_collection(p,dict(stages=[],scoped_cleanup_verified=True))
        self.assertTrue(any('stages' in e for e in errors))
        result=dict(stages=[dict(s,index=i,status='passed',probes=[],forwarding={}) for i,s in enumerate(p['stages'])])
        self.assertTrue(any('probe' in e for e in validate_collection(p,result)))
        result['stages'][0]['rate_mbps']+=1
        self.assertTrue(any('plan' in e for e in validate_collection(p,result)))

    def test_applied_queue_must_match_not_just_requested_value(self):
        check_queue('qdisc netem 10: parent 5:1 limit 100 delay 2ms',100)
        with self.assertRaises(ValueError):check_queue('qdisc netem 10: limit 1000 delay 2ms',100)

    @unittest.skipUnless(importlib.util.find_spec('networkx'),'Requires controller Python with NetworkX')
    def test_physical_capacity_override_preserves_dijkstra_and_other_links(self):
        from phase6c import apply_capacities
        from topology_state import TopologyState
        from test_topology_state import activate
        from telemetry import link_snapshot
        topology=TopologyState();activate(topology)
        before=topology.path(1,4)
        apply_capacities(topology,{'s2-s5':50},.1)
        self.assertEqual(topology.path(1,4),before)
        self.assertEqual(topology.graph[2][5]['base_delay'],2)
        self.assertEqual(topology.graph[2][5]['capacity']*.1,50)
        self.assertEqual(topology.graph[1][2]['capacity']*.1,100)
        links,ports=link_snapshot(topology,{},.1)
        self.assertEqual(ports[2,4]['capacity_mbps'],50)
        with self.assertRaises(ValueError):apply_capacities(topology,{'s2-s5':30,'unknown':10},.1)
        self.assertEqual(topology.graph[2][5]['capacity']*.1,50)


if __name__=='__main__':unittest.main()

class ProbeClockRegressionTests(unittest.TestCase):
    def test_monotonic_coverage_preserves_wall_evidence_and_rejects_missing_or_short_coverage(self):
        plan=make_plan();stages=[]
        for expected in plan['stages']:
            start=expected['planned_elapsed_seconds'];seconds=expected['seconds']
            probes=[dict(source=src,destination=dst,complete=True,exit_code=0,
                transmitted=round(seconds/plan['probe_interval_seconds']),
                started=1000+start,scheduled_last_send=1000+start+seconds-.2,
                started_monotonic=start,scheduled_last_send_monotonic=start+seconds-.2)
                for src,dst in plan['flow_pairs']]
            stages.append(dict(expected,status='passed',probes=probes,
                forwarding={'-'.join(pair):{} for pair in plan['flow_pairs']},
                traffic_started=1000+start,traffic_finished=1000+start+seconds+3,
                traffic_started_monotonic=start,traffic_finished_monotonic=start+seconds))
        result=dict(stages=stages,scoped_cleanup_verified=True,owned_processes_cleaned=True,tc_verified=True)
        original=copy.deepcopy(result)
        self.assertEqual(validate_collection(plan,result),[])
        self.assertEqual(result,original)
        for missing in ('started_monotonic','scheduled_last_send_monotonic'):
            broken=copy.deepcopy(result);broken['stages'][0]['probes'][0].pop(missing)
            self.assertTrue(any('missing probe coverage' in e for e in validate_collection(plan,broken)))
        broken=copy.deepcopy(result);broken['stages'][0]['probes'][0]['scheduled_last_send_monotonic']=1
        self.assertTrue(any('missing probe coverage' in e for e in validate_collection(plan,broken)))
