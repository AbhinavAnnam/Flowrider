"""Phase 5 behavior tests: real OF1.3 encoders, deterministic transport/clock."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from os_ken.ofproto import ofproto_v1_3_parser as parser
from topology_config import HOSTS
from topology_state import TopologyState
from test_topology_state import activate
from test_controller import Datapath, event
from predictive_routing import RoutingManager, RoutingPolicy
from unittest.mock import patch


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.topology=TopologyState(); activate(self.topology)
        self.dps={i:Datapath(i) for i in self.topology.switches}
        self.now=100.;self.records=[]
        self.router=RoutingManager(self.topology,self.dps,clock=lambda:self.now,
            record=lambda kind,row:self.records.append((kind,row)))
        self.a,self.b=HOSTS[1]['mac'],HOSTS[4]['mac']

    def pair(self,a=None,b=None):
        return self.router.pairs[self.router.key(a or self.a,b or self.b)]

    def finish(self,limit=100):
        # Switches acknowledge operations and return exact entries requested by
        # the engine. A separate packet-level transport test exercises codecs.
        for _ in range(limit):
            requests=list(self.router.requests.items())
            if not requests:return
            for (dpid,xid),request in requests:
                dp=self.dps[dpid]
                if request['kind']=='barrier':self.router.barrier_reply(event(dp,xid=xid).msg)
                else:
                    rows=[]
                    for rule in request['expected']:
                        encoded=bytearray()
                        instruction=parser.OFPInstructionActions(dp.ofproto.OFPIT_APPLY_ACTIONS,
                            self.router.actions(rule,dp))
                        instruction.serialize(encoded,0)
                        rows.append(NS(cookie=rule['cookie'],priority=rule['priority'],table_id=0,
                            match=parser.OFPMatch(**rule['match']),
                            instructions=[] if getattr(self,'ovs_drop',False) and rule['out_port'] is None else
                                [parser.OFPInstruction.parser(encoded,0)],packet_count=3,byte_count=180,
                            idle_timeout=rule['idle_timeout'],hard_timeout=rule['hard_timeout']))
                    self.router.stats_reply(event(dp,xid=xid,flags=0,body=rows).msg)
        self.fail('Transaction did not terminate')

    def initial(self):
        self.router.ensure(self.a,self.b)
        self.finish()
        self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])

    def baseline_mode(self):
        self.router=RoutingManager(self.topology,self.dps,clock=lambda:self.now,
            record=lambda kind,row:self.records.append((kind,row)),routing_mode='baseline',trial_id='baseline-trial')

    def test_static_mode_weights_are_base_delays_without_forecasts(self):
        self.baseline_mode()
        graph=self.router._weighted()
        self.assertEqual(len(graph.edges),7)
        for _,_,edge in graph.edges(data=True):self.assertEqual(edge['ml_weight'],edge['base_delay'])
        self.telemetry(['s1-s2','s2-s5'])
        for _,_,edge in self.router._weighted().edges(data=True):self.assertEqual(edge['ml_weight'],edge['base_delay'])

    def test_baseline_installs_verified_rules_and_ignores_congested_predictions(self):
        self.baseline_mode();self.router.ensure(self.a,self.b)
        self.assertIsNone(self.pair()['installed'])
        self.finish();cookie=self.pair()['installed']['cookie']
        self.now+=31;self.telemetry(['s1-s2','s2-s5']);self.now+=5
        self.telemetry(['s1-s2','s2-s5']);self.router.tick();self.finish()
        self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])
        self.assertEqual(self.pair()['installed']['cookie'],cookie)
        self.assertEqual(self.pair()['observed']['cookie'],cookie)
        self.assertTrue(any(isinstance(m,parser.OFPFlowMod) and m.priority==20 for dp in self.dps.values() for m in dp.messages))
        self.assertFalse(any(r['kind']=='reroute_candidate' or r.get('reason')=='predicted_congestion' for k,r in self.records if k=='event'))
        snapshot=self.router.snapshot()
        self.assertEqual(snapshot['routing_mode'],'baseline');self.assertFalse(snapshot['automatic_rerouting'])
        self.assertEqual(snapshot['trial_id'],'baseline-trial');self.assertTrue(snapshot['topology_version'])

    def test_baseline_topology_failure_and_restoration_reinstall_shortest_path(self):
        self.baseline_mode();self.initial()
        self.topology.set_port(2,4,'s2-eth4',False);self.router.topology_changed()
        self.assertIsNone(self.pair()['installed'])
        self.router.ensure(self.a,self.b);self.finish()
        self.assertNotEqual(self.pair()['installed']['path'],[1,2,5,4])
        self.assertEqual(self.pair()['installed']['path'],self.topology.path(1,4))
        self.topology.set_port(2,4,'s2-eth4',True);self.router.topology_changed()
        self.router.ensure(self.a,self.b);self.finish()
        self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])

    def test_baseline_retains_audits_and_hard_timeout_refresh(self):
        self.baseline_mode();self.initial();cookie=self.pair()['installed']['cookie']
        self.now+=6;self.router.tick();self.finish()
        self.assertEqual(self.pair()['observed']['cookie'],cookie)
        self.now+=95;self.router.tick();self.finish()
        self.assertNotEqual(self.pair()['installed']['cookie'],cookie)
        self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])

    def test_baseline_rejects_missing_readback_instead_of_claiming_installation(self):
        self.baseline_mode();self.router.ensure(self.a,self.b)
        for (dpid,xid),request in list(self.router.requests.items()):
            self.router.barrier_reply(event(self.dps[dpid],xid=xid).msg)
        (dpid,xid),request=next(iter(self.router.requests.items()))
        self.router.stats_reply(event(self.dps[dpid],xid=xid,flags=0,body=[]).msg)
        self.assertIsNone(self.pair()['installed'])
        self.assertTrue(any(r.get('reason')=='missing_flow_entries' for kind,r in self.records if kind=='event'))
        self.finish();self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])

    def test_both_modes_use_existing_dijkstra_with_expected_weights(self):
        from network_topology import calculate_shortest_path
        for mode in ('baseline','predictive_ml'):
            with self.subTest(mode=mode):
                self.setUp();self.router=RoutingManager(self.topology,self.dps,clock=lambda:self.now,routing_mode=mode)
                self.initial();self.now+=31
                with patch('predictive_routing.calculate_shortest_path',wraps=calculate_shortest_path) as shortest:
                    self.telemetry(['s1-s2','s2-s5'])
                    graph,src,dst,weight=shortest.call_args.args
                    self.assertEqual((src,dst,weight),(1,4,'ml_weight'))
                    self.assertEqual(graph[1][2]['ml_weight'],5 if mode=='baseline' else 5*(1+25*.95**4))
                self.now+=5;self.telemetry(['s1-s2','s2-s5']);self.finish()
                self.assertEqual(self.pair()['installed']['path'],[1,2,5,4] if mode=='baseline' else [1,6,5,4])

    def test_ovs_drop_readback_allows_recovery_after_missing_rule(self):
        self.initial();self.ovs_drop=True
        self.router.transit_miss(self.a,self.b,self.pair()['installed']['vlan'])
        self.finish()
        self.assertEqual(self.pair()['status'],'active')
        self.assertFalse(self.pair()['guarded'])
        self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])

    def test_missing_rule_recovers_selected_path_without_policy_reroute(self):
        self.reroute();path=list(self.pair()['installed']['path']);self.ovs_drop=True
        self.router.transit_miss(self.a,self.b,self.pair()['installed']['vlan'])
        self.finish()
        self.assertEqual(self.pair()['installed']['path'],path)

    def test_quiet_reverse_direction_does_not_expire_active_pair(self):
        self.initial()
        # Model switch timers: only forward rules see packets for 40 seconds.
        # Returning traffic must still find every reverse transit/egress rule.
        rules=self.pair()['installed']['rules']
        reverse=[r for r in rules if r['match']['eth_src']==self.b]
        surviving=[r for r in reverse if not r['idle_timeout'] or r['idle_timeout']>40]
        self.assertEqual(len(surviving),len(reverse))
        self.assertTrue(all(r['hard_timeout']==120 for r in rules))

    def test_bidirectional_packet_delivery_after_reverse_silence_and_hard_refresh(self):
        from test_predictive_socket import Fabric
        self.initial();fabric=Fabric();loaded=None;installed_at=self.now
        for elapsed in (0,10,20,31,40,60,101,130):
            self.now=100+elapsed;self.router.tick();self.finish()
            version=self.pair()['installed']
            if loaded!=version['cookie']:
                loaded=version['cookie'];installed_at=self.now
                fabric.peers={d:NS(rules={}) for d in self.dps}
                for i,r in enumerate(version['rules']):
                    fabric.peers[r['dpid']].rules[i]=dict(priority=r['priority'],match=r['match'],
                        actions=self.router.actions(r,self.dps[r['dpid']]),packets=0,
                        idle=r['idle_timeout'],hard=r['hard_timeout'],last_used=self.now)
            for peer in fabric.peers.values():
                peer.rules={k:r for k,r in peer.rules.items() if
                    (not r['idle'] or self.now-r['last_used']<r['idle']) and self.now-installed_at<r['hard']}
            self.assertEqual(fabric.forward(1,self.a,self.b),[1,2,5,4])
            for peer in fabric.peers.values():
                for r in peer.rules.values():
                    if r['match']['eth_src']==self.a:r['last_used']=self.now
            if elapsed==0 or elapsed>=40:
                self.assertEqual(fabric.forward(4,self.b,self.a),[4,5,2,1])
                for peer in fabric.peers.values():
                    for r in peer.rules.values():
                        if r['match']['eth_src']==self.b:r['last_used']=self.now
        self.assertEqual(version['reason'],'refresh_hard_timeout')
        self.assertFalse(fabric.errors)

    def test_drop_normalization_does_not_accept_output_or_clear_actions(self):
        dp=self.dps[1]
        empty=parser.OFPInstructionActions(dp.ofproto.OFPIT_APPLY_ACTIONS,[])
        self.assertEqual(self.router._action_signature([empty]),self.router._action_signature([]))
        for instruction in (parser.OFPInstructionActions(dp.ofproto.OFPIT_CLEAR_ACTIONS,[]),
                parser.OFPInstructionActions(dp.ofproto.OFPIT_APPLY_ACTIONS,[parser.OFPActionOutput(1)])):
            self.assertNotEqual(self.router._action_signature([instruction]),self.router._action_signature([]))

    def telemetry(self,high=(),valid=True,age=0):
        observations=[];predictions=[]
        from datetime import datetime,timezone,timedelta
        wall=datetime.now(timezone.utc)
        for u,v,e in self.topology.graph.edges(data=True):
            link=f's{min(u,v)}-s{max(u,v)}';value=.95 if link in high else .05
            observations.append(dict(link_id=link,valid=True,link_active=e['active'],
                utilization=.4,observation_timestamp=wall.isoformat(),source='live_mininet',session_id='test'))
            predictions.append(dict(link_id=link,status='valid' if valid else 'model_not_ready',
                predicted_utilization=value if valid else None,observation_timestamp=wall.isoformat(),
                target_timestamp=(wall+timedelta(seconds=15)).isoformat(),prediction_horizon_seconds=15,
                model_version='test-rf',source='live_mininet',session_id='test'))
        self.router.telemetry(dict(epoch=self.router.epoch,monotonic_time=self.now-age,
            observations=observations,predictions=predictions,source='live_mininet'))

    def reroute(self):
        self.initial();self.now+=31
        self.telemetry(['s1-s2','s2-s5']);self.now+=5;self.telemetry(['s1-s2','s2-s5']);self.finish()

    def test_baseline_install_waits_for_flow_entries_not_just_barriers(self):
        self.router.ensure(self.a,self.b)
        self.assertIsNone(self.pair()['installed'])
        for (dpid,xid),request in list(self.router.requests.items()):
            self.router.barrier_reply(event(self.dps[dpid],xid=xid).msg)
        self.assertIsNone(self.pair()['installed'])
        self.assertTrue(all(r['kind']=='stats' for r in self.router.requests.values()))
        self.finish();self.assertEqual(self.pair()['observed']['method'],'openflow_flow_stats')

    def test_no_change_when_baseline_remains_optimal(self):
        self.initial();self.now+=31
        for _ in range(3):self.telemetry();self.now+=5
        self.assertIsNone(self.pair()['transaction'])
        self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])

    def test_reroutes_both_directions_with_correct_ports_and_version_tags(self):
        self.reroute();version=self.pair()['installed']
        self.assertEqual(version['path'],[1,6,5,4])
        forward=[r for r in version['rules'] if r['match']['eth_src']==self.a]
        self.assertEqual([(r['dpid'],r['match']['in_port'],r['out_port']) for r in forward],
            [(1,1,3),(6,3,2),(5,3,2),(4,3,1)])
        self.assertEqual(forward[0]['match']['vlan_vid'],0)
        self.assertTrue(all(r['match']['vlan_vid']==0x1000|version['vlan'] for r in forward[1:]))
        self.assertEqual(len(version['rules']),8)
        self.assertEqual(self.pair()['observed']['path'],version['path'])

    def test_unrelated_pairs_are_not_deleted_by_a_reroute(self):
        self.initial();other=(HOSTS[2]['mac'],HOSTS[3]['mac'])
        self.router.ensure(*other);self.finish();cookie=self.pair(*other)['installed']['cookie']
        self.now+=31;self.telemetry(['s1-s2','s2-s5']);self.now+=5;self.telemetry(['s1-s2','s2-s5']);self.finish()
        self.now+=2;self.router.tick();self.finish()
        deletes=[m for dp in self.dps.values() for m in dp.messages if isinstance(m,parser.OFPFlowMod)
                 and m.command==dp.ofproto.OFPFC_DELETE]
        self.assertFalse(any(m.cookie==cookie for m in deletes))
        self.assertEqual(self.pair(*other)['installed']['cookie'],cookie)
        self.assertTrue(all(m.cookie_mask==0xffffffffffffffff for m in deletes))

    def test_missing_or_stale_predictions_fall_back_to_verified_baseline(self):
        for stale in (False,True):
            with self.subTest(stale=stale):
                self.setUp();self.reroute();self.now+=2
                self.now+=11 if stale else 0
                self.telemetry(['s1-s2','s2-s5'],valid=stale,age=11 if stale else 0);self.router.tick();self.finish()
                self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])
                self.assertIn('fallback',self.pair()['installed']['reason'])

    def test_cooldown_confirmation_and_minimum_improvement_prevent_churn(self):
        self.initial();self.telemetry(['s1-s2','s2-s5']);self.now+=5;self.telemetry(['s1-s2','s2-s5'])
        self.assertIsNone(self.pair()['transaction'])
        self.now+=31;self.telemetry(['s1-s2','s2-s5']);self.assertIsNone(self.pair()['transaction'])
        self.now+=5;self.telemetry(['s1-s2','s2-s5']);self.finish()
        cookie=self.pair()['installed']['cookie']
        self.now+=1;self.telemetry(['s1-s6','s5-s6']);self.now+=5;self.telemetry(['s1-s6','s5-s6'])
        self.assertEqual(self.pair()['installed']['cookie'],cookie)
        self.assertIsNone(self.pair()['transaction'])

    def test_disconnected_path_is_rejected_and_old_epoch_predictions_ignored(self):
        self.initial();old=self.router.epoch
        self.topology.set_port(2,4,'s2-eth4',False);self.router.topology_changed()
        self.assertIsNone(self.pair()['installed']);self.assertIsNone(self.pair()['observed'])
        self.assertFalse(self.router.telemetry(dict(epoch=old,source='live_mininet')))
        self.router.ensure(self.a,self.b);self.finish()
        self.assertNotEqual(self.pair()['installed']['path'],[1,2,5,4])
        self.topology.unregister_switch(1);self.router.topology_changed()
        self.assertFalse(self.router.ensure(self.a,self.b))

    def test_partial_install_failure_is_not_reported_active_and_recovers(self):
        self.initial();self.now+=31;self.telemetry(['s1-s2','s2-s5']);self.now+=5;self.telemetry(['s1-s2','s2-s5'])
        key,request=next(iter(self.router.requests.items()))
        self.assertTrue(self.router.error(event(self.dps[key[0]],xid=key[1],type=3,code=1).msg))
        self.assertIsNone(self.pair()['installed']);self.assertIsNone(self.pair()['observed'])
        self.finish()
        self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])
        self.assertTrue(any(r.get('kind')=='installation_failed' for k,r in self.records if k=='event'))

    def test_missing_entries_wrong_actions_and_timeout_are_failures(self):
        self.router.ensure(self.a,self.b)
        for (dpid,xid),request in list(self.router.requests.items()):self.router.barrier_reply(event(self.dps[dpid],xid=xid).msg)
        (dpid,xid),request=next(iter(self.router.requests.items()))
        self.router.stats_reply(event(self.dps[dpid],xid=xid,flags=0,body=[]).msg)
        self.assertIsNone(self.pair()['installed']);self.finish()
        self.assertIsNotNone(self.pair()['installed'])
        self.now+=31;self.telemetry(['s1-s2','s2-s5']);self.now+=5;self.telemetry(['s1-s2','s2-s5']);self.now+=4;self.router.tick()
        self.assertIsNone(self.pair()['installed'])
        self.finish();self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])

    def test_snapshot_distinguishes_recommendation_installation_and_verification(self):
        self.initial();self.now+=31;self.telemetry(['s1-s2','s2-s5']);self.now+=5;self.telemetry(['s1-s2','s2-s5'])
        snapshot=self.router.snapshot();json.dumps(snapshot,allow_nan=False)
        route=snapshot['routes'][0]
        self.assertEqual(route['recommended_route']['path'],[1,6,5,4])
        self.assertEqual(route['installed_route']['path'],[1,2,5,4])
        self.assertEqual(snapshot['topology']['connected_switches'],[1,2,3,4,5,6])
        self.assertEqual(len(snapshot['topology']['links']),7)
        self.finish();route=self.router.snapshot()['routes'][0]
        self.assertEqual(route['installed_route']['path'],route['observed_route']['path'])
        self.assertFalse(route['observed_route']['end_to_end_delivery_verified'])

    def test_improvement_threshold_and_policy_validation(self):
        self.router.policy=RoutingPolicy(min_improvement=.9999)
        self.initial();self.now+=31
        for _ in range(3):self.telemetry(['s1-s2','s2-s5']);self.now+=5
        self.assertIsNone(self.pair()['transaction'])
        with self.assertRaises(ValueError):RoutingPolicy(cooldown=-1)
        with self.assertRaises(ValueError):RoutingPolicy(prediction_max_age=float('nan'))

    def test_stats_from_old_datapath_and_duplicates_cannot_activate(self):
        self.router.ensure(self.a,self.b)
        (dpid,xid),request=next(iter(self.router.requests.items()))
        self.assertFalse(self.router.barrier_reply(event(Datapath(dpid),xid=xid).msg))
        self.assertIsNone(self.pair()['installed']);self.finish()
        self.assertFalse(self.router.barrier_reply(event(self.dps[dpid],xid=xid).msg))

    def test_wrong_output_action_is_rejected_by_exact_readback(self):
        self.router.ensure(self.a,self.b)
        for (dpid,xid),request in list(self.router.requests.items()):self.router.barrier_reply(event(self.dps[dpid],xid=xid).msg)
        (dpid,xid),request=next(iter(self.router.requests.items()));dp=self.dps[dpid]
        rows=[NS(cookie=r['cookie'],priority=r['priority'],table_id=0,
            match=parser.OFPMatch(**r['match']),idle_timeout=r['idle_timeout'],hard_timeout=r['hard_timeout'],
            instructions=[parser.OFPInstructionActions(dp.ofproto.OFPIT_APPLY_ACTIONS,[parser.OFPActionOutput(99)])],
            packet_count=0,byte_count=0) for r in request['expected']]
        self.router.stats_reply(event(dp,xid=xid,flags=0,body=rows).msg)
        self.assertIsNone(self.pair()['installed']);self.assertEqual(self.pair()['status'],'recovering')
        self.finish();self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])

    def test_idle_expiration_deactivates_and_next_packet_reinstalls(self):
        self.initial();cookie=self.pair()['installed']['cookie'];dp=self.dps[1]
        self.router.flow_removed(event(dp,cookie=cookie,reason=dp.ofproto.OFPRR_IDLE_TIMEOUT).msg)
        self.finish();self.assertIsNone(self.pair()['installed'])
        self.assertFalse(self.pair()['guarded']);self.now+=2
        self.router.ensure(self.a,self.b);self.finish();self.assertIsNotNone(self.pair()['installed'])

    def test_periodic_audit_and_hard_timeout_refresh(self):
        self.initial();cookie=self.pair()['installed']['cookie']
        self.now+=6;self.router.tick();self.finish()
        self.assertEqual(self.pair()['installed']['cookie'],cookie)
        self.now+=95;self.router.tick();self.finish()
        self.assertNotEqual(self.pair()['installed']['cookie'],cookie)
        self.assertEqual(self.pair()['installed']['reason'],'refresh_hard_timeout')

    def test_same_path_hard_refresh_does_not_restart_rerouting_cooldown(self):
        self.initial();last_change=self.pair()['last_change']
        self.now+=101;self.router.tick();self.finish()
        self.assertEqual(self.pair()['last_change'],last_change)
        self.telemetry(['s1-s2','s2-s5']);self.now+=5
        self.telemetry(['s1-s2','s2-s5']);self.finish()
        self.assertEqual(self.pair()['installed']['path'],[1,6,5,4])
        self.assertEqual(self.pair()['installed']['reason'],'predicted_congestion')

    def test_cleanup_failure_quarantines_only_affected_pair_then_retries(self):
        self.initial();self.now+=31;self.telemetry(['s1-s2','s2-s5']);self.now+=5;self.telemetry(['s1-s2','s2-s5'])
        (d,x),_=next(iter(self.router.requests.items()));self.router.error(event(self.dps[d],xid=x).msg)
        (d,x),_=next(iter(self.router.requests.items()));self.router.error(event(self.dps[d],xid=x).msg)
        self.assertEqual(self.pair()['status'],'quarantined');self.assertIsNone(self.pair()['installed'])
        self.assertFalse(self.router.requests)
        self.now+=6;self.router.tick();self.finish()
        self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])

    def test_repeated_frame_and_mixed_model_versions_do_not_trigger(self):
        self.initial();self.now+=31;self.telemetry(['s1-s2','s2-s5'])
        self.assertEqual(self.pair()['count'],1)
        self.telemetry(['s1-s2','s2-s5']);self.assertEqual(self.pair()['count'],1)
        self.router.predictions['s1-s2']['model_version']='different-model'
        self.assertIsNone(self.router._weighted())

    def test_predictions_expiring_during_staging_cannot_be_cut_over(self):
        self.initial();self.now+=31;self.telemetry(['s1-s2','s2-s5']);self.now+=5;self.telemetry(['s1-s2','s2-s5'])
        # Keep each OpenFlow operation within its deadline but let the forecast
        # wall timestamp become stale before the ingress cutover.
        from datetime import timedelta
        from prediction import timestamp
        for p in self.router.predictions.values():
            p['observation_timestamp']=(timestamp(p['observation_timestamp'])-timedelta(seconds=11)).isoformat()
        self.finish();self.assertEqual(self.pair()['installed']['path'],[1,2,5,4])
        self.assertTrue(any(r.get('reason')=='predictions_expired_before_cutover' for k,r in self.records if k=='event'))

    def test_failed_obsolete_delete_keeps_tag_reserved_and_waits_before_retry(self):
        from unittest.mock import patch
        self.reroute();context=next(iter(self.router.retired.values()));old=context['version']
        self.now+=2
        send=self.dps[1].send_msg
        def reject_delete(message):
            if isinstance(message,parser.OFPFlowMod) and message.command==self.dps[1].ofproto.OFPFC_DELETE:
                raise OSError('delete transport temporarily unavailable')
            return send(message)
        with patch.object(self.dps[1],'send_msg',side_effect=reject_delete):
            self.router.tick()
        self.assertEqual(context['phase'],'queued')
        self.assertTrue(all(r['context'] is not context for r in self.router.requests.values()))
        self.assertIn(old['vlan'],self.router.vlans)
        self.now+=6;self.router.tick();self.finish()
        self.assertNotIn(old['cookie'],self.router.retired)
        self.assertNotIn(old['vlan'],self.router.vlans)
