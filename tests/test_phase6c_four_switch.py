"""Four-switch experiment acceptance contracts; fixtures are never live results."""
import copy
import importlib.util
from pathlib import Path
import unittest

from phase6c import make_four_switch_plan, validate_plan, expected_tc, forecast_metrics
from topology_config import topology_definition, wired_links, TOPOLOGY_VERSION


class FourSwitchContracts(unittest.TestCase):
    def test_independent_wiring_delay_capacity_and_host_access(self):
        switches, hosts, version = topology_definition('phase6c')
        self.assertEqual(set(switches), {1, 2, 3, 4})
        self.assertNotEqual(version, TOPOLOGY_VERSION)
        edges = {(l['src'], l['dst']): l for l in wired_links('phase6c')}
        self.assertEqual(set(edges), {(1, 2), (2, 4), (1, 3), (3, 4)})
        self.assertEqual(sum(edges[e]['base_delay'] for e in ((1, 2), (2, 4))), 4)
        self.assertEqual(sum(edges[e]['base_delay'] for e in ((1, 3), (3, 4))), 10)
        self.assertEqual(edges[2, 4]['capacity'] * .1, 20)
        self.assertEqual(hosts[1]['switch'], 1)
        self.assertEqual(hosts[2]['switch'], 4)
        self.assertEqual(hosts[3]['switch'], 2)
        self.assertEqual(hosts[4]['switch'], 4)
        access = {(h['switch'], h['port']) for h in hosts.values()}
        transit = {(l[e], l[e + '_port']) for l in edges.values() for e in ('src', 'dst')}
        self.assertFalse(access & transit)

    def test_identical_seeded_schedule_full_and_feasibility(self):
        for profile in ('full', 'feasibility'):
            p = make_four_switch_plan(profile=profile)
            self.assertEqual(p, make_four_switch_plan(profile=profile))
            validate_plan(p)
            self.assertEqual(p['flow_pairs'], [['h1', 'h2'], ['h3', 'h4']])
            self.assertLess(2*p['stages'][0]['rate_mbps'], 20)
            sustained = next(s for s in p['stages'] if s['scenario']=='sustained')
            self.assertGreater(2*sustained['rate_mbps'], 20)
            self.assertGreaterEqual(sustained['seconds'], 45)
            self.assertLess(max(2*s['rate_mbps'] for s in p['stages']), 100)
            bad=copy.deepcopy(p);bad['link_capacities_mbps']['s2-s4']=200
            with self.assertRaises(ValueError): validate_plan(bad)
        self.assertLess(make_four_switch_plan(profile='feasibility')['seconds'], make_four_switch_plan()['seconds'])

    def test_all_tc_endpoints_and_real_bottleneck_interfaces(self):
        endpoints=expected_tc(make_four_switch_plan())
        self.assertEqual(len(endpoints), 16)
        self.assertEqual({k for k,v in endpoints.items() if v[0]==20}, {'s2-eth3','s4-eth2'})
        self.assertEqual(endpoints['s4-eth4'], (100,0,None))
        self.assertEqual(endpoints['h4-eth0'], (100,0,None))

    @unittest.skipUnless(importlib.util.find_spec('networkx'), 'Controller Python required')
    def test_static_path_and_snapshot_use_selected_topology(self):
        from topology_state import TopologyState
        from predictive_routing import RoutingManager
        from telemetry import link_snapshot
        state=TopologyState('phase6c')
        for dpid,name in state.configured_switches.items():
            state.register_switch(dpid)
            state.replace_ports(dpid,{p:(f'{name}-eth{p}',True) for p in state.expected_ports(dpid)})
        self.assertEqual(state.path(1,4), [1,2,4])
        self.assertEqual(state.path(2,4), [2,4])
        links,_=link_snapshot(state,{},.1)
        self.assertEqual(next(l for l in links if l['link_id']=='s2-s4')['capacity_mbps'],20)
        manager=RoutingManager(state,{})
        self.assertEqual(manager.snapshot()['topology_version'],state.topology_version)
        self.assertEqual(len(manager.snapshot()['topology']['links']),4)
        self.assertEqual(len(TopologyState().graph),6)

    def test_accuracy_matches_horizon_and_lead_is_not_assumed(self):
        observations=[dict(link_id='s2-s4',valid=True,monotonic_elapsed_time=t,utilization=u)
                      for t,u in ((0,.2),(5,.2),(10,.2),(15,.9),(20,.9),(35,.1))]
        predictions=[dict(link_id='s2-s4',status='valid',target_monotonic_elapsed_time=15,
                          predicted_utilization=.85,observation_timestamp='2026-10-28T00:00:00+00:00')]
        result=forecast_metrics(observations,predictions)
        self.assertEqual(result['matched_count'],1)
        self.assertAlmostEqual(result['mae'],.05)
        self.assertEqual(result['lead_times_seconds'],[15])
        self.assertEqual(forecast_metrics(observations,[])['status'],'unavailable_no_matched_targets')


if __name__=='__main__':unittest.main()

@unittest.skipUnless(importlib.util.find_spec('sklearn'), 'Frozen model Python required')
class FrozenModelCompatibility(unittest.TestCase):
    def test_existing_rf_accepts_real_feature_builder_without_model_changes(self):
        import hashlib
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
        from check_phase6c_model import diagnose
        model=Path(__file__).resolve().parents[1]/'data/research-20261015T180515Z/evaluation/model.pkl'
        before=hashlib.sha256(model.read_bytes()).hexdigest();result=diagnose(model)
        self.assertEqual(result['model_sha256'],before)
        self.assertEqual(hashlib.sha256(model.read_bytes()).hexdigest(),before)
        self.assertEqual(result['metadata']['horizon_seconds'],15)
        self.assertEqual(result['training_feature_ranges']['capacity_mbps']['minimum'],100)
        self.assertEqual(len(result['probes']),10)
        for probe in result['probes']:
            self.assertEqual(len(probe['features']),10)
            self.assertTrue(0<=probe['predicted_utilization']<=1)
        saturated=next(p for p in result['probes'] if p['capacity_mbps']==20 and p['regime']=='saturated')
        self.assertFalse(saturated['congestion_predicted'])  # measured diagnostic, not guaranteed live behavior

@unittest.skipUnless(importlib.util.find_spec('os_ken'), 'Controller Python required')
class FourSwitchForwardingPolicy(unittest.TestCase):
    def harness(self,mode):
        from test_predictive_routing import RoutingTests
        from test_controller import Datapath
        from topology_state import TopologyState
        from predictive_routing import RoutingManager
        h=RoutingTests();h.setUp();h.topology=TopologyState('phase6c')
        for dpid,name in h.topology.configured_switches.items():
            h.topology.register_switch(dpid)
            h.topology.replace_ports(dpid,{p:(f'{name}-eth{p}',True) for p in h.topology.expected_ports(dpid)})
        h.dps={i:Datapath(i) for i in h.topology.switches}
        h.router=RoutingManager(h.topology,h.dps,clock=lambda:h.now,routing_mode=mode,
            record=lambda kind,row:h.records.append((kind,row)))
        h.a,h.b='00:00:00:00:00:01','00:00:00:00:00:02'
        h.router.ensure(h.a,h.b);h.finish()
        return h

    def test_qualifying_forecast_policy_contract_and_bidirectional_packet_forwarding(self):
        # Synthetic high forecasts test the forwarding contract only. They are
        # never used by the benchmark or represented as saved-model inference.
        from types import SimpleNamespace
        from test_predictive_socket import Fabric
        for mode in ('baseline','predictive_ml'):
            h=self.harness(mode)
            self.assertEqual(h.pair()['installed']['path'],[1,2,4])
            h.now+=31;h.telemetry(['s2-s4'])
            self.assertEqual(h.router._weighted()[2][4]['ml_weight'],
                2 if mode=='baseline' else 2*(1+25*.95**4))
            self.assertEqual(h.pair()['installed']['path'],[1,2,4])
            h.now+=5;h.telemetry(['s2-s4']);h.finish()
            path=[1,2,4] if mode=='baseline' else [1,3,4]
            self.assertEqual(h.pair()['installed']['path'],path)
            fabric=Fabric(profile='phase6c');fabric.peers={d:SimpleNamespace(rules={}) for d in h.dps}
            for i,rule in enumerate(h.pair()['installed']['rules']):
                fabric.peers[rule['dpid']].rules[i]=dict(priority=rule['priority'],match=rule['match'],
                    actions=h.router.actions(rule,h.dps[rule['dpid']]),packets=0)
            self.assertEqual(fabric.forward(1,h.a,h.b),path)
            self.assertEqual(fabric.forward(4,h.b,h.a),list(reversed(path)))
            self.assertEqual(fabric.errors,[])

class MeasurementRejectionTests(unittest.TestCase):
    def test_empty_ping_count_is_rejected_and_negative_rtt_cannot_parse(self):
        from phase6c import parse_ping
        for text,count in [('0 packets transmitted, 0 received,',0),
                ('[1.0] 64 bytes icmp_seq=1 time=-1 ms\n1 packets transmitted, 1 received,',1)]:
            with self.assertRaises(ValueError):parse_ping(text,count)

class AuditWindowRegressionTests(unittest.TestCase):
    def test_short_stages_cover_multiple_unchanged_five_second_audits_and_refresh(self):
        # The real failure had no audit in stage 7 and a zero-counter hard
        # refresh in stage 4. A single audit-period stage cannot establish two
        # same-cookie observations. Retain a third interval for refresh/phase.
        for profile in ('full','feasibility'):
            plan=make_four_switch_plan(profile=profile)
            self.assertGreaterEqual(min(s['seconds'] for s in plan['stages']),3*plan['measurement_interval_seconds'])
            self.assertGreaterEqual(plan.get('telemetry_tail_seconds',0),15+plan['measurement_interval_seconds'])

    def test_historical_measurement_contract_remains_readable(self):
        plan=make_four_switch_plan(profile='feasibility',contract_version=1)
        self.assertEqual(plan['seconds'],135)
        self.assertNotIn('measurement_contract_version',plan)
        self.assertEqual(next(s['seconds'] for s in plan['stages'] if s['scenario']=='burst'),5)
        validate_plan(plan)
