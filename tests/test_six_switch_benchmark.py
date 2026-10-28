"""Short six-switch preset; no live experiment is launched by these tests."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from phase6c import make_six_switch_plan,stage_flows,validate_plan,expected_tc

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('six_switch_runner',ROOT/'scripts/run_phase6c.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class SixSwitchPlanTests(unittest.TestCase):
    def test_schedule_has_three_shared_backbone_flows_without_capacity_or_queue_overrides(self):
        plan=make_six_switch_plan()
        validate_plan(plan)
        self.assertEqual(plan,make_six_switch_plan())
        self.assertEqual(plan['seconds'],210)
        self.assertEqual([s['seconds'] for s in plan['stages']],[30,30,120,30])
        self.assertEqual(plan['flow_pairs'],[['h1','h4'],['h2','h5'],['h3','h6']])
        self.assertEqual(stage_flows(plan,plan['stages'][2]),[('h1','h4',60),('h2','h5',80),('h3','h6',80)])
        self.assertEqual(sum(rate for _,_,rate in stage_flows(plan,plan['stages'][2])),220)
        self.assertEqual(plan['link_capacities_mbps'],{})
        self.assertEqual(plan['bottleneck']['bandwidth_mbps'],200)
        self.assertEqual(plan['bottleneck']['queue_packets'],1000)
        self.assertEqual(expected_tc(plan)['s2-eth4'],(200,2,1000))
        bad=copy.deepcopy(plan);bad['flow_rates_mbps']['sustained'][1]=99
        with self.assertRaises(ValueError):validate_plan(bad)

    @unittest.skipUnless(importlib.util.find_spec('networkx'),'Controller Python required')
    def test_all_fixed_delay_paths_cross_backbone_and_detours_are_available(self):
        from topology_state import TopologyState
        from test_topology_state import activate
        import networkx as nx
        topology=TopologyState();activate(topology)
        for (src,dst),expected,detour in zip(((1,4),(2,5),(3,6)),([1,2,5,4],[2,5],[3,2,5,6]),
                ([1,6,5,4],[2,3,4,5],[3,4,5,6])):
            self.assertEqual(topology.path(src,dst),expected)
            self.assertIn({2,5},[{u,v} for u,v in zip(expected,expected[1:])])
            for u,v in zip(detour,detour[1:]):
                self.assertTrue(topology.graph[u][v]['active'])
                self.assertEqual(topology.graph[u][v]['capacity']*.1,100)

    def test_cli_uses_only_two_trials_and_the_existing_topology(self):
        with patch.object(runner,'run_benchmark') as start:
            runner.main(['--profile','six-switch'])
        args=start.call_args.args[0]
        self.assertEqual(args.trials,2)
        self.assertEqual(args.topology,'six')
        self.assertEqual(args.queue_packets,1000)
        with patch.object(runner,'run_benchmark') as repeated:
            runner.main(['--profile','six-switch','--trials','4'])
        self.assertEqual(repeated.call_args.args[0].trials,4)
        self.assertEqual(runner.ORDER,('baseline','predictive_ml','predictive_ml','baseline'))

    def test_console_preset_never_starts_four_switch_preflight_or_graph_analysis(self):
        calls=[]
        def run(command,**kwargs):
            calls.append(command)
            if '--init' in command:
                folder=Path(command[command.index('--experiment')+1]);folder.mkdir();(folder/'experiment.json').write_text('{}')
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as directory,patch.object(runner.os,'geteuid',return_value=0), \
             patch.object(runner,'controller_python',return_value='/fixture/python'), \
             patch.object(runner,'resource_check',return_value={}),patch.object(runner.shutil,'which',return_value='/fixture'), \
             patch.object(runner.subprocess,'run',side_effect=run),patch.object(runner,'print_console') as output:
            runner.main(['--profile','six-switch','--experiment',str(Path(directory)/'new')])
        trials=[c for c in calls if '--trial' in c]
        self.assertEqual([c[-1] for c in trials],['1','2'])
        self.assertTrue(all('six-switch' in c for c in trials))
        self.assertFalse(any('matplotlib' in str(c) or 'check_phase6c_model.py' in str(c) or '/analyze.py' in str(c) for c in calls))
        output.assert_called_once()

class RoutingReasonTests(unittest.TestCase):
    def test_gate_confirmation_cooldown_selection_and_installation_failures_are_distinct(self):
        from datetime import datetime,timezone
        stamp=lambda t:datetime.fromtimestamp(1700000000+t,timezone.utc).isoformat()
        pair='pair:one';base=[1,2,5,4];detour=[1,6,5,4]
        links=[dict(source_dpid=u,destination_dpid=v,routing_weight=weight,active=True,
                    prediction=dict(predicted_utilization=.7))
               for u,v,weight in ((1,2,10),(2,5,10),(5,4,5),(1,6,5),(6,5,5))]
        route=dict(route_id=pair,source_mac='00:00:00:00:00:01',destination_mac='00:00:00:00:00:04',
            installed_route=dict(path=base),recommended_route=dict(path=detour,policy='predicted',timestamp=stamp(40)),transaction_phase=None)
        snap=dict(timestamp=stamp(40),policy=dict(cooldown=30,min_improvement=.1),topology=dict(links=links),routes=[route])
        events=[dict(kind='route_activated',timestamp=stamp(0),route_id=pair,reason='initial_baseline',previous_path=None,path=base)]
        result=runner.routing_reasons([snap],events,[['h1','h4']],1700000000,1700000100)
        self.assertEqual(result['h1-h4']['prediction gate'],1)
        for link in links:link['prediction']['predicted_utilization']=.95
        result=runner.routing_reasons([snap],events,[['h1','h4']],1700000000,1700000100)
        self.assertEqual(result['h1-h4']['confirmation'],1)
        events[0]['timestamp']=stamp(20)
        result=runner.routing_reasons([snap],events,[['h1','h4']],1700000000,1700000100)
        self.assertEqual(result['h1-h4']['cooldown'],1)
        route['recommended_route']['path']=base
        result=runner.routing_reasons([snap],events,[['h1','h4']],1700000000,1700000100)
        self.assertEqual(result['h1-h4']['route selection'],1)
        events.append(dict(kind='installation_failed',route_id=pair,timestamp=stamp(42)))
        result=runner.routing_reasons([snap],events,[['h1','h4']],1700000000,1700000100)
        self.assertEqual(result['h1-h4']['installation failure'],1)

@unittest.skipUnless(importlib.util.find_spec('networkx'),'Controller Python required')
class SixSwitchAnalysisTests(unittest.TestCase):
    def trials(self,root):
        from test_phase6c_analysis import fixture,a
        plan=make_six_switch_plan()
        write=lambda path,value:path.write_text(json.dumps(value))
        write(root/'experiment.json',dict(trial_count=2,plan_hash=runner.digest(plan),plan=plan,
            model_sha256='fixture-only-model-hash',source_sha256={'fixture':'not-live'}))
        trials=[]
        for index in (1,2):
            trial=fixture(root,index,plan);result=a.validate_trial(trial)
            self.assertEqual(result['errors'],[])
            write(trial/'validation.json',result);trials.append(trial)
        return trials,a

    def test_native_trial_validation_console_and_heterogeneous_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);trials,a=self.trials(root)
            before=a.b.inventory(root);output=io.StringIO()
            with patch('sys.stdout',output):runner.print_console(root)
            text=output.getvalue()
            self.assertIn('Average RTT (ms)',text)
            self.assertIn('S2-S5 RF',text)
            self.assertIn('no verified ML reroute',text)
            self.assertEqual(a.b.inventory(root),before)
            path=trials[0]/'traffic/traffic.json';data=json.loads(path.read_text());data['stages'][2]['flows'][1]['rate_mbps']=60
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'iperf settings differ'):
                a.validate_trial(trials[0])
            raw=trials[0]/'traffic/02-h2-h5.json';document=json.loads(raw.read_text())
            document['start']['test_start']['target_bitrate']=60e6;raw.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError,'Per-flow traffic rates differ'):
                a.validate_trial(trials[0])

    def test_baseline_requires_real_congestion_and_spare_detour_capacity(self):
        from test_phase6c_analysis import fixture,a
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            root=fixture(Path(directory),1,make_six_switch_plan())
            with sqlite3.connect(root/'telemetry.sqlite') as connection:
                for key,encoded in connection.execute('SELECT id,record_json FROM directions').fetchall():
                    row=json.loads(encoded)
                    if (row['source_dpid'],row['destination_dpid'])==(3,4):
                        row['tx_mbps']=90
                        connection.execute('UPDATE directions SET record_json=? WHERE id=?',(json.dumps(row),key))
            self.assertTrue(any('no demonstrated spare capacity' in e for e in a.validate_trial(root)['errors']))
