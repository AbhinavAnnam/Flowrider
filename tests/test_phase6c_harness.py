"""No-root harness isolation and TC boundary tests; no lab is created."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from collect_phase6c import capture_tc, interface_tc
from phase6c import make_plan
from topology_config import wired_links

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('phase6c_launcher',ROOT/'scripts/run_phase6c.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)


class Phase6CHarnessTests(unittest.TestCase):
    def test_plan_only_cli_never_overwrites_and_refuses_out_of_order_trial_before_sudo(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);model=root/'fixture-only.pkl';model.write_bytes(b'not a model; init test only')
            experiment=root/'experiment'
            command=[sys.executable,str(ROOT/'scripts/run_phase6c.py'),'--experiment',str(experiment),'--model',str(model)]
            init=subprocess.run(command+['--init'],capture_output=True,text=True,timeout=5)
            self.assertEqual(init.returncode,0,init.stderr)
            original=(experiment/'experiment.json').read_bytes()
            self.assertEqual(json.loads(original)['order'],['baseline','predictive_ml','predictive_ml','baseline'])
            rerun=subprocess.run(command+['--init'],capture_output=True,text=True,timeout=5)
            self.assertNotEqual(rerun.returncode,0)
            self.assertEqual((experiment/'experiment.json').read_bytes(),original)
            skipped=subprocess.run(command+['--trial','2'],capture_output=True,text=True,timeout=5)
            self.assertNotEqual(skipped.returncode,0)
            self.assertIn('Run ABBA in order',skipped.stderr)
            self.assertEqual(list(experiment.iterdir()),[experiment/'experiment.json'])

    def test_owned_process_stop_reaps_and_escalates_with_bounded_waits(self):
        process=SimpleNamespace(returncode=None)
        process.poll=lambda:process.returncode
        waits=[]
        def wait(timeout):
            waits.append(timeout)
            if process.returncode is None:raise subprocess.TimeoutExpired('fixture',timeout)
            return process.returncode
        process.wait=wait;process.terminate=lambda:None
        def kill():process.returncode=-9
        process.kill=kill
        self.assertEqual(launcher.stop_owned(process,45),-9)
        self.assertEqual(waits,[45,5])

    def test_tc_only_reconfigures_the_two_backbone_interfaces(self):
        class Interface:
            def __init__(self,name,node,rate,delay,queue):
                self.name=name;self.node=SimpleNamespace(name=node)
                self.rate=rate;self.delay=delay;self.queue=queue
            def config(self,bw,delay,max_queue_size):
                self.rate=bw;self.delay=float(delay[:-2]);self.queue=max_queue_size
        links=[]
        for link in wired_links():
            ends=[Interface(f"s{link[end]}-eth{link[end+'_port']}",f"s{link[end]}",link['capacity']*.1,link['base_delay'],1000)
                  for end in ('src','dst')]
            links.append(SimpleNamespace(intf1=ends[0],intf2=ends[1]))
        for h in range(1,7):
            links.append(SimpleNamespace(intf1=Interface(f's{h}-eth1',f's{h}',100,0,None),
                                         intf2=Interface(f'h{h}-eth0',f'h{h}',100,0,None)))
        def actual(interface,kind):
            if kind=='class':return f'class htb 5:1 rate {interface.rate}Mbit'
            return f'qdisc netem 10: limit {interface.queue} delay {interface.delay}ms' if interface.delay else 'qdisc htb'
        net=SimpleNamespace(links=links)
        with patch('collect_phase6c.interface_tc',side_effect=actual):records=capture_tc(net,make_plan(),apply=True)
        self.assertEqual(len(records),26)
        self.assertEqual({r['interface'] for r in records if r['bandwidth_mbps']==50},{'s2-eth4','s5-eth4'})
        self.assertTrue(all(r['bandwidth_mbps']==100 for r in records if r['interface'] not in ('s2-eth4','s5-eth4')))

    def test_host_tc_executes_in_the_interface_namespace(self):
        # Real subprocess verifies output/reaping while the fake node selects
        # its namespace boundary; host TC is never sent to the root command helper.
        calls=[]
        def popen(command,**kwargs):
            calls.append(command)
            return subprocess.Popen([sys.executable,'-c','print("namespace tc fixture")'],**kwargs)
        interface=SimpleNamespace(name='h1-eth0',node=SimpleNamespace(popen=popen))
        self.assertEqual(interface_tc(interface,'class'),'namespace tc fixture\n')
        self.assertEqual(calls,[['tc','-s','class','show','dev','h1-eth0']])


if __name__=='__main__':unittest.main()

class FourSwitchLauncherTests(unittest.TestCase):
    def test_single_command_dispatches_complete_benchmark(self):
        with patch.object(launcher,'run_benchmark') as benchmark:
            launcher.main(['--trials','4'])
        args=benchmark.call_args.args[0]
        self.assertEqual(args.trials,4)
        self.assertEqual(args.topology,'phase6c')
        self.assertEqual(args.profile,'full')

    def test_plan_only_default_is_four_switch_and_short_profile_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            model=Path(directory)/'fixture.pkl';model.write_bytes(b'init only')
            root=Path(directory)/'raw'
            result=subprocess.run([sys.executable,str(ROOT/'scripts/run_phase6c.py'),
                '--experiment',str(root),'--model',str(model),'--init','--profile','feasibility'],
                capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            plan=json.loads((root/'experiment.json').read_text())['plan']
            self.assertEqual(plan['topology'],'phase6c')
            self.assertEqual(plan['bottleneck']['bandwidth_mbps'],20)
            self.assertEqual(plan['profile'],'feasibility')

class AutomaticSeriesTests(unittest.TestCase):
    def run_fake(self,directory,fail=False):
        calls=[]
        def run(command,**kwargs):
            calls.append(command)
            if '--init' in command:
                root=Path(command[command.index('--experiment')+1]);root.mkdir()
                (root/'experiment.json').write_text('{}')
            if fail and '--trial' in command:
                raise subprocess.CalledProcessError(1,command)
            return SimpleNamespace(returncode=0)
        with patch.object(launcher.os,'geteuid',return_value=0), \
             patch.object(launcher,'controller_python',return_value=sys.executable), \
             patch.object(launcher,'resource_check',return_value={'test_fixture':True}), \
             patch.object(launcher.shutil,'which',return_value='/fixture'), \
             patch.object(launcher.subprocess,'run',side_effect=run):
            if fail:
                with self.assertRaises(subprocess.CalledProcessError):
                    launcher.main(['--experiment',str(Path(directory)/'raw'),'--trials','4'])
            else:launcher.main(['--experiment',str(Path(directory)/'raw'),'--trials','4'])
        return calls

    def test_feasibility_precedes_full_abba_with_identical_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=self.run_fake(directory)
        trials=[c for c in calls if '--trial' in c]
        self.assertEqual([int(c[c.index('--trial')+1]) for c in trials],[1,2,1,2,3,4])
        self.assertTrue(all('--topology' in c and c[c.index('--topology')+1]=='phase6c' for c in trials))
        self.assertEqual([c[c.index('--profile')+1] for c in trials],['feasibility']*2+['full']*4)
        self.assertEqual(len([c for c in calls if any(str(v).endswith('/analyze.py') for v in c)]),2)

    def test_failed_feasibility_writes_analysis_and_prevents_full_trials(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=self.run_fake(directory,fail=True)
        self.assertEqual(len([c for c in calls if '--trial' in c]),1)
        analyses=[c for c in calls if any(str(v).endswith('/analyze.py') for v in c)]
        self.assertEqual(len(analyses),1)
        self.assertIn('feasibility-analysis',analyses[0][-1])

class RefreshedCounterProofTests(unittest.TestCase):
    def test_zero_readback_seeds_comparison_but_only_later_positive_increases_prove_forwarding(self):
        from collect_phase6c import record_forwarding
        import copy
        route=dict(installed_route=dict(cookie=7,path=[1,2,4]),observed_route=dict(cookie=7,
            counters=[dict(dpid=d,source_mac=f'00:00:00:00:00:{source:02x}',packet_count=0)
                      for source in (1,2) for d in (1,2,4)]))
        stage=dict(forwarding={},bidirectional_forwarding={});prior={}
        record_forwarding(stage,route,'h1','h2',prior)
        self.assertFalse(stage['forwarding']);self.assertFalse(stage['bidirectional_forwarding'])
        later=copy.deepcopy(route)
        for counter in later['observed_route']['counters']:counter['packet_count']=5
        record_forwarding(stage,later,'h1','h2',prior)
        self.assertEqual(stage['forwarding']['h1-h2']['starting_counters'],{1:0,2:0,4:0})
        self.assertEqual(stage['forwarding']['h1-h2']['ending_counters'],{1:5,2:5,4:5})
        self.assertIn('h1-h2',stage['bidirectional_forwarding'])

    def test_missing_hops_wrong_cookie_or_unchanged_counts_cannot_seed_or_pass(self):
        from collect_phase6c import record_forwarding
        for observed in (dict(cookie=6,counters=[]),dict(cookie=7,counters=[
                dict(dpid=1,source_mac='00:00:00:00:00:01',packet_count=3)])):
            stage=dict(forwarding={},bidirectional_forwarding={});prior={}
            record_forwarding(stage,dict(installed_route=dict(cookie=7,path=[1,2,4]),observed_route=observed),'h1','h2',prior)
            self.assertEqual(prior,{})
            self.assertFalse(stage['forwarding']);self.assertFalse(stage['bidirectional_forwarding'])

    def test_feasibility_burst_records_same_cookie_increases_after_a_midstage_refresh(self):
        from collect_phase6c import record_forwarding
        from phase6c import make_four_switch_plan
        duration=next(s['seconds'] for s in make_four_switch_plan(profile='feasibility')['stages'] if s['scenario']=='burst')
        stage=dict(forwarding={},bidirectional_forwarding={});prior={}
        # At 4.8s a hard refresh verifies a new cookie with empty transit counters.
        # Only the next audit at 10.8s can prove traffic on that version.
        for when,cookie,count in ((0,7,100),(4.8,8,0),(10.8,8,5)):
            if when>duration:continue
            route=dict(installed_route=dict(cookie=cookie,path=[1,2,4]),observed_route=dict(cookie=cookie,
                counters=[dict(dpid=d,source_mac=f'00:00:00:00:00:{src:02x}',packet_count=count)
                    for src in (1,2) for d in (1,2,4)]))
            record_forwarding(stage,route,'h1','h2',prior)
        for key in ('forwarding','bidirectional_forwarding'):
            proof=stage[key]['h1-h2']
            self.assertEqual(proof['route']['installed_route']['cookie'],8)
            self.assertTrue(all(proof['ending_counters'][d]>value for d,value in proof['starting_counters'].items()))
