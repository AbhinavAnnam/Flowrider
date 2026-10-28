"""Synthetic temporary fixtures exercise real SQLite/JSON analysis, never live results."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import sqlite3
import subprocess
import sys
import unittest

from phase6c import (PAIRS, create_trial, make_plan, parse_ping, validate_collection, write_json)
from telemetry_store import TelemetryStore
from topology_config import HOSTS, wired_links, topology_definition
from phase6c import stage_flows

spec=importlib.util.spec_from_file_location('phase6c_analysis',Path(__file__).parents[1]/'docs/phase6c/analyze.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)


def stamp(seconds):return datetime.fromtimestamp(1700000000+seconds,timezone.utc).isoformat()


def fixture(root,index=1,plan=None):
    """A complete baseline *test fixture*, with known UDP/loss/RTT arithmetic."""
    plan=plan or make_plan();profile=plan.get('topology','six');hosts=topology_definition(profile)[1]
    quick=plan.get('profile')=='six-switch';bidirectional=profile=='phase6c' or quick
    pairs=[(int(src[1:]),int(dst[1:])) for src,dst in plan['flow_pairs']]
    manifest=create_trial(root,index,plan,'fixture-only-model-hash')
    manifest['source_sha256']={'fixture':'not-live'}
    root=Path(manifest['output']);traffic=root/'traffic';traffic.mkdir()
    config=dict(routing_mode=manifest['routing_mode'],trial_id=manifest['trial_id'],traffic_configuration=plan,
        topology_version=plan['topology_version'],bandwidth_scale=.1,experiment_link_capacities_mbps=plan['link_capacities_mbps'])
    store=TelemetryStore(root/'telemetry.sqlite','live_mininet',config)
    links=[]
    for l in wired_links(profile):
        u,v=l['src'],l['dst'];links.append(dict(link_id=f's{min(u,v)}-s{max(u,v)}',source_dpid=u,destination_dpid=v,
            ports={str(u):l['src_port'],str(v):l['dst_port']},active=True,
            capacity_mbps=plan['bottleneck']['bandwidth_mbps'] if f's{min(u,v)}-s{max(u,v)}'==plan['bottleneck']['link_id'] else 100,base_delay_ms=l['base_delay']))
    routes=[]
    for index,(src,dst) in enumerate(pairs,1):
        path=([1,2,4] if src==1 else [2,4]) if profile=='phase6c' else {1:[1,2,5,4],2:[2,5],3:[3,2,5,6]}[src]
        route=dict(route_id=f'fixture-{src}-{dst}',source_mac=HOSTS[src]['mac'],destination_mac=HOSTS[dst]['mac'],
            baseline_path=path,recommended_route=dict(path=path,policy='baseline',timestamp=stamp(0)),installed_route=dict(path=path,cookie=index),
            observed_route=dict(cookie=index,counters=[dict(dpid=d,source_mac=HOSTS[src]['mac'],packet_count=10) for d in path]))
        if bidirectional:
            route['observed_route']['counters'] += [dict(dpid=d,source_mac=hosts[dst]['mac'],packet_count=10) for d in path]
        routes.append(route)
        store.write_routing('event',dict(timestamp=stamp(0),kind='route_activated',reason='initial_baseline',
            route_id=route['route_id'],path=path,previous_path=None,cookie=index,verification='openflow_flow_stats'))
    for elapsed in range(0,plan['seconds']+1,5):
        observations=[];predictions=[];directions=[]
        for link in links:
            utilization=.1 if (profile=='phase6c' or quick) and link['link_id']!=plan['bottleneck']['link_id'] else .9
            observations.append(dict(link_id=link['link_id'],observation_timestamp=stamp(elapsed),monotonic_elapsed_time=elapsed,
                valid=True,link_active=True,utilization=utilization,tx_mbps=utilization*link['capacity_mbps'],rx_mbps=0,capacity_mbps=link['capacity_mbps'],status='valid'))
            predictions.append(dict(link_id=link['link_id'],observation_timestamp=stamp(elapsed),
                status='valid',model_version='fixture-only',predicted_utilization=.9,raw_predicted_utilization=.9,
                congestion_threshold=.8,prediction_horizon_seconds=15,target_timestamp=stamp(elapsed+15),
                target_monotonic_elapsed_time=elapsed+15,current_utilization=utilization))
            if quick:
                for u,v in ((link['source_dpid'],link['destination_dpid']),(link['destination_dpid'],link['source_dpid'])):
                    directions.append(dict(link_id=link['link_id'],observation_timestamp=stamp(elapsed),valid=True,
                        source_dpid=u,destination_dpid=v,tx_mbps=0,capacity_mbps=link['capacity_mbps']))
        store.write_batch([],directions,observations,predictions)
        store.write_routing('snapshot',dict(timestamp=stamp(elapsed),topology_epoch=12,policy={},routes=routes,
            topology=dict(connected_switches=list(topology_definition(profile)[0]),switches=[],hosts=[],links=links)))
    store.close();stages=[];captures=[]
    for index,planned in enumerate(plan['stages']):
        t=planned['planned_elapsed_seconds'];seconds=planned['seconds'];probes=[];flows=[];forwarding={}
        for (src,dst,rate),route in zip(stage_flows(plan,planned),routes):
            count=seconds*5
            text=''.join(f'[{1700000000+t+i*.2:.3f}] 64 bytes: icmp_seq={i+1} ttl=64 time=20.0 ms\n' for i in range(count))
            text+=f'{count} packets transmitted, {count} received, 0% packet loss, time {seconds*1000}ms\n'
            file=f'{index:02d}-{src}-{dst}.ping.log';(traffic/file).write_text(text)
            probes.append(dict(source=src,destination=dst,file=file,expected_count=count,exit_code=0,complete=True,
                started=1700000000+t,scheduled_last_send=1700000000+t+seconds-.2,**parse_ping(text,count)))
            file=f'{index:02d}-{src}-{dst}.json'
            data=dict(start=dict(test_start=dict(protocol='UDP',duration=seconds,target_bitrate=rate*1e6),
                connected=[dict(local_host='10.0.0.'+src[1:],remote_host='10.0.0.'+dst[1:])],version='fixture',system_info='fixture'),
                end=dict(sum_sent=dict(bytes=1000*seconds,seconds=seconds),
                    sum_received=dict(bytes=1000*seconds,seconds=seconds,packets=seconds,lost_packets=0)))
            write_json(traffic/file,data);flows.append(dict(source=src,destination=dst,rate_mbps=rate,iperf_json=file))
            forwarding[f'{src}-{dst}']=dict(route=route,
                starting_counters={str(d):1 for d in route['installed_route']['path']},
                ending_counters={str(d):10 for d in route['installed_route']['path']})
        stage=dict(planned,index=index,status='passed',probes=probes,flows=flows,forwarding=forwarding,
            started_timestamp=stamp(t),finished_timestamp=stamp(t+seconds),actual_elapsed_seconds=t,
            traffic_started=1700000000+t,traffic_finished=1700000000+t+seconds)
        stages.append(stage);ovs={}
        for dpid in topology_definition(profile)[0]:
            ovs[f's{dpid}']='\n'.join(f"cookie=0x{r['installed_route']['cookie']:x}, n_packets=10, dl_src={r['source_mac']},dl_dst={r['destination_mac']} actions=output:2"
                for r in routes if dpid in r['installed_route']['path'])
        if bidirectional:
            stage['bidirectional_forwarding']=copy.deepcopy(forwarding)
            for dpid in topology_definition(profile)[0]:
                ovs[f's{dpid}']+='\n'+'\n'.join(f"cookie=0x{r['installed_route']['cookie']:x}, n_packets=10, dl_src={r['destination_mac']},dl_dst={r['source_mac']} actions=output:2" for r in routes if dpid in r['installed_route']['path'])
        captures.append(dict(timestamp=stamp(t+5),stage=index,ovs=ovs,tc={('s2-eth3' if profile=='phase6c' else 's2-eth4'):'dropped 3, backlog 20Kb 10p'}))
    collection=dict(status='collected',stages=stages,tc_verified=True,scoped_cleanup_verified=True,owned_processes_cleaned=True)
    write_json(root/'collection.json',collection)
    write_json(traffic/'traffic.json',dict(stages=stages,session_id=store.session_id,routing_mode=manifest['routing_mode'],trial_id=manifest['trial_id'],
        seed=plan['seed'],bandwidth_scale=.1,requested_traffic_seconds=plan['seconds'],elapsed_seconds=plan['seconds']))
    write_json(root/'trial-status.json',dict(status='passed',collector_exit_code=0,controller_exit_code=-15,controller_stop_requested=True,model_unchanged=True,source_unchanged=True))
    write_json(root/'trial.json',manifest)
    from phase6c import expected_tc
    tc=[dict(interface=name,bandwidth_mbps=rate,delay_ms=delay,queue_packets=queue,
             classes=f'class htb 5:1 rate {rate}Mbit',
             qdisc=f'qdisc netem 10: limit {queue} delay {delay}ms' if delay else 'qdisc htb')
        for name,(rate,delay,queue) in expected_tc(plan).items()]
    write_json(root/'applied-tc.json',tc);write_json(root/'final-tc.json',tc)
    (root/'forwarding-tc.jsonl').write_text(''.join(json.dumps(c)+'\n' for c in captures))
    (root/'controller.log').write_text('Test fixture only; not a live controller log\n')
    return root


class Phase6CAnalysisTests(unittest.TestCase):
    def late_route_fixture(self, directory):
        from phase6c import make_six_switch_plan
        root=fixture(Path(directory),index=2,plan=make_six_switch_plan())
        cookie=0x4652000000000041;path=[3,4,5,6]
        with sqlite3.connect(root/'telemetry.sqlite') as c:
            session=c.execute('SELECT session_id FROM sessions').fetchone()[0]
            event=dict(kind='route_activated',timestamp=stamp(208.4),route_id='fixture-3-6',
                path=path,previous_path=[3,2,5,6],cookie=cookie,reason='predicted_congestion',
                verification='openflow_flow_stats')
            c.execute('INSERT INTO routing_events(session_id,timestamp,record_json) VALUES (?,?,?)',
                (session,event['timestamp'],json.dumps(event)))
            row=c.execute('SELECT id,record_json FROM routing_snapshots ORDER BY id DESC LIMIT 1').fetchone()
            snap=json.loads(row[1]);route=snap['routes'][2]
            route['installed_route']=dict(cookie=cookie,path=path)
            route['observed_route']=dict(cookie=cookie,counters=[dict(dpid=d,source_mac=HOSTS[3]['mac'],packet_count=0) for d in path])
            c.execute('UPDATE routing_snapshots SET record_json=? WHERE id=?',(json.dumps(snap),row[0]))
            # The next positive controller audit is outside the traffic window.
            snap['timestamp']=stamp(215)
            for counter in route['observed_route']['counters']:counter['packet_count']=100
            c.execute('INSERT INTO routing_snapshots(session_id,timestamp,record_json) VALUES (?,?,?)',
                (session,snap['timestamp'],json.dumps(snap)))
        capture=dict(timestamp=stamp(209.9),stage=3,tc={},ovs={f's{d}':
            f'cookie=0x{cookie:x}, n_packets=1037, dl_src={HOSTS[3]["mac"]},dl_dst={HOSTS[6]["mac"]} actions=output:2'
            for d in path})
        with (root/'forwarding-tc.jsonl').open('a') as f:f.write(json.dumps(capture)+'\n')
        return root,capture

    def test_late_changed_version_uses_in_stage_independent_counters_before_next_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root,_=self.late_route_fixture(directory);before=a.b.inventory(root)
            result=a.validate_trial(root)
            self.assertEqual(result['errors'],[])
            self.assertEqual(result['summary']['routing']['tracked_changes_positive_hops'],1)
            proof=result['summary']['route_changes'][0]['forwarding_evidence']
            self.assertEqual(proof['minimum_hop_packets'],1037)
            self.assertEqual(proof['actual_source_host'],3)
            self.assertEqual(a.b.inventory(root),before)

    def test_late_version_still_requires_exact_cookie_direction_every_hop_and_stage_time(self):
        for defect in ('zero','missing','cookie','reverse','duplicate','after_stage','before_activation','wrong_stage','unverified'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                root,capture=self.late_route_fixture(directory)
                if defect=='zero':capture['ovs']['s6']=capture['ovs']['s6'].replace('n_packets=1037','n_packets=0')
                elif defect=='missing':del capture['ovs']['s6']
                elif defect=='cookie':capture['ovs']['s6']=capture['ovs']['s6'].replace('0000000041','0000000042')
                elif defect=='reverse':capture['ovs']['s6']=capture['ovs']['s6'].replace(
                    'dl_src='+HOSTS[3]['mac']+',dl_dst='+HOSTS[6]['mac'],
                    'dl_src='+HOSTS[6]['mac']+',dl_dst='+HOSTS[3]['mac'])
                elif defect=='duplicate':capture['ovs']['s6']+='\n'+capture['ovs']['s6']
                elif defect=='after_stage':capture['timestamp']=stamp(210.1)
                elif defect=='before_activation':capture['timestamp']=stamp(208.3)
                elif defect=='wrong_stage':capture['stage']=2
                elif defect=='unverified':
                    with sqlite3.connect(root/'telemetry.sqlite') as c:
                        row=c.execute('SELECT id,record_json FROM routing_events ORDER BY id DESC LIMIT 1').fetchone()
                        event=json.loads(row[1]);event['verification']=None
                        c.execute('UPDATE routing_events SET record_json=? WHERE id=?',(json.dumps(event),row[0]))
                file=root/'forwarding-tc.jsonl';lines=file.read_text().splitlines()
                file.write_text('\n'.join(lines[:-1]+[json.dumps(capture)])+'\n')
                self.assertIn('Measured-pair changed route lacks positive counters in actual traffic direction',
                    a.validate_trial(root)['errors'])

    def test_read_only_revalidation_rechecks_prior_verdict_but_retains_runtime_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root,_=self.late_route_fixture(directory)
            status=json.loads((root/'trial-status.json').read_text())
            status.update(status='failed',validation_error='Measured-pair changed route lacks positive counters in actual traffic direction')
            write_json(root/'trial-status.json',status);before=a.b.inventory(root)
            command=[sys.executable,a.__file__,'--experiment',str(root),'--validate-only']
            result=subprocess.run(command,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            self.assertIn('PASS',result.stdout)
            self.assertIn(status['validation_error'],result.stdout)
            self.assertEqual(a.b.inventory(root),before)
            status['error']='Collector runtime exception';write_json(root/'trial-status.json',status)
            result=subprocess.run(command,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,1)
            self.assertIn('Recorded trial failure',result.stdout)

    def test_read_only_database_retains_committed_wal_without_touching_raw_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);database=root/'wal.sqlite'
            c=sqlite3.connect(database);c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE committed (value INTEGER)');c.execute('INSERT INTO committed VALUES (42)');c.commit()
            before=a.b.inventory(root)
            with a.b.readonly_database(database) as copy_connection:
                self.assertEqual(copy_connection.execute('SELECT value FROM committed').fetchone(),(42,))
            self.assertEqual(a.b.inventory(root),before)
            c.close()

    def test_complete_fixture_validates_and_missing_evidence_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root=fixture(Path(directory));before=a.b.inventory(root)
            result=a.validate_trial(root)
            self.assertEqual(result['errors'],[])
            self.assertEqual(result['summary']['rtt']['average_ms'],20)
            self.assertEqual(result['summary']['routing']['path_changes'],0)
            self.assertEqual(a.b.inventory(root),before)
            collection=json.loads((root/'collection.json').read_text())
            collection['stages'][3]['probes'].pop();write_json(root/'collection.json',collection)
            self.assertTrue(any('probe' in e for e in a.validate_trial(root)['errors']))

    def test_unrelated_ovs_cookie_and_runtime_errors_are_not_successes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=fixture(Path(directory))
            path=root/'forwarding-tc.jsonl';path.write_text(path.read_text().replace('cookie=0x1,','cookie=0xdead,'))
            (root/'controller.log').write_text('Telemetry backlog full; dropped batch (1 total), forwarding continues\n')
            errors=a.validate_trial(root)['errors']
            self.assertTrue(any('OVS' in e for e in errors));self.assertTrue(any('Controller errors' in e for e in errors))

    def test_equal_trial_weighting_and_variation_not_pooled_sample_weight(self):
        metrics=dict(icmp_loss_percent=0,sender_mbps=10,receiver_mbps=9,wall_aggregate_goodput_mbps=18,
            loss_percent=1,congestion_link_seconds=2)
        summaries=[dict(routing_mode='baseline',rtt=dict(average_ms=value,median_ms=value,p95_ms=value,maximum_ms=value),**metrics)
            for value in (10,30)]
        results=a.aggregate(summaries)
        self.assertEqual(results['baseline']['metrics']['average_ms']['mean'],20)
        self.assertAlmostEqual(results['baseline']['metrics']['average_ms']['sample_sd'],2**.5*10)
        self.assertIsNone(results['predictive_ml']['metrics']['average_ms']['mean'])

    def test_wrong_ring_delay_and_missing_sustained_pressure_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=fixture(Path(directory))
            path=root/'applied-tc.json';tc=json.loads(path.read_text());tc[0]['delay_ms']=3;write_json(path,tc)
            self.assertTrue(any('TC metadata' in e for e in a.validate_trial(root)['errors']))
            tc[0]['delay_ms']=5;write_json(path,tc)
            path=root/'forwarding-tc.jsonl';path.write_text(path.read_text().replace('backlog 20Kb 10p','backlog 0b 0p'))
            self.assertTrue(any('congestion not demonstrated' in e for e in a.validate_trial(root)['errors']))

    @unittest.skipUnless(importlib.util.find_spec('matplotlib'),'Requires analysis Python with matplotlib')
    def test_complete_abba_analysis_writes_actual_fixture_figures_and_preserves_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory);root=parent/'raw';root.mkdir()
            plan=make_plan();write_json(root/'experiment.json',dict(plan_hash=a.digest(plan),
                model_sha256='fixture-only-model-hash',source_sha256={'fixture':'not-live'}))
            for index in range(1,5):fixture(root,index)
            before=a.b.inventory(root);output=parent/'analysis'
            result=subprocess.run([sys.executable,str(Path(a.__file__)),'--experiment',str(root),'--output',str(output)],
                capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            data=json.loads((output/'results.json').read_text())
            self.assertEqual(data['status'],'complete')
            self.assertEqual(data['per_mode']['baseline']['trial_count'],2)
            self.assertTrue((output/'average_ms.png').is_file())
            self.assertTrue((output/'routing-timeline.csv').is_file())
            self.assertEqual(a.b.inventory(root),before)


if __name__=='__main__':unittest.main()

class FourSwitchAnalysisTests(unittest.TestCase):
    def test_four_switch_trial_validates_and_rejects_missing_reverse_evidence(self):
        from phase6c import make_four_switch_plan
        with tempfile.TemporaryDirectory() as directory:
            root=fixture(Path(directory),plan=make_four_switch_plan(profile='feasibility'))
            result=a.validate_trial(root)
            self.assertEqual(result['errors'],[])
            self.assertEqual(result['summary']['telemetry_coverage_percent'],100)
            self.assertEqual(len(result['summary']['alternative_path_utilization']),2)
            collection=json.loads((root/'collection.json').read_text())
            del collection['stages'][0]['bidirectional_forwarding']
            write_json(root/'collection.json',collection)
            self.assertTrue(any('reverse' in e for e in a.validate_trial(root)['errors']))

    @unittest.skipUnless(importlib.util.find_spec('matplotlib'),'Analysis Python required')
    def test_paired_feasibility_generates_report_and_all_graphs(self):
        from phase6c import make_four_switch_plan
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory);root=parent/'raw';root.mkdir();plan=make_four_switch_plan(profile='feasibility')
            write_json(root/'experiment.json',dict(trial_count=2,plan_hash=a.digest(plan),
                model_sha256='fixture-only-model-hash',source_sha256={'fixture':'not-live'}))
            for index in (1,2):fixture(root,index,plan)
            output=parent/'analysis'
            result=subprocess.run([sys.executable,str(Path(a.__file__)),'--experiment',str(root),'--output',str(output)],
                capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            for name in ('average_ms','p95_ms','goodput','loss_percent','udp_loss','congestion','routing','utilization'):
                self.assertTrue((output/f'{name}.png').is_file(),name)
            self.assertIn('COMPLETE PAIRED FEASIBILITY',(output/'report.md').read_text())

class TopologyEvidenceTests(unittest.TestCase):
    def test_wrong_controller_link_delay_is_rejected_even_when_tc_is_correct(self):
        from phase6c import make_four_switch_plan
        with tempfile.TemporaryDirectory() as directory:
            root=fixture(Path(directory),plan=make_four_switch_plan(profile='feasibility'))
            with sqlite3.connect(root/'telemetry.sqlite') as connection:
                rows=connection.execute('SELECT id,record_json FROM routing_snapshots').fetchall()
                for key,encoded in rows:
                    record=json.loads(encoded);record['topology']['links'][0]['base_delay_ms']=7
                    connection.execute('UPDATE routing_snapshots SET record_json=? WHERE id=?',(json.dumps(record),key))
            self.assertTrue(any('Snapshot link configuration' in e for e in a.validate_trial(root)['errors']))

class MissingForwardingEvidenceTests(unittest.TestCase):
    def test_missing_stage_forwarding_is_rejected_without_masking_collector_failure(self):
        from phase6c import make_four_switch_plan
        for index in (1,2):  # baseline and predictive ML
            with self.subTest(mode=index),tempfile.TemporaryDirectory() as directory:
                root=fixture(Path(directory),index,make_four_switch_plan(profile='feasibility'))
                path=root/'collection.json';collection=json.loads(path.read_text())
                collection['stages'][0]['forwarding'].pop('h1-h2')
                write_json(path,collection)
                errors=a.validate_trial(root)['errors']
                self.assertTrue(any('missing forwarding evidence' in e for e in errors))
                self.assertTrue(any('Missing saved exact positive per-hop readback' in e for e in errors))
                self.assertTrue(any('missing independent positive OVS rules' in e for e in errors))

    def test_missing_uninstalled_or_empty_route_never_passes_independent_dump_verification(self):
        for route in ({},{'installed_route':None},{'installed_route':{'cookie':1,'path':[]}}):
            with self.subTest(route=route):
                self.assertFalse(a.verified_dump({'ovs':{}},route,1,2))

class TruncatedPlanRegressionTests(unittest.TestCase):
    def test_aborted_rtt_collection_reports_missing_stages_and_original_probe_error(self):
        from phase6c import make_four_switch_plan
        for index in (1,2):
            with self.subTest(mode=index),tempfile.TemporaryDirectory() as directory:
                root=fixture(Path(directory),index,make_four_switch_plan(profile='feasibility'))
                collection=json.loads((root/'collection.json').read_text())
                collection.update(status='failed',error='RuntimeError: Incomplete RTT measurement')
                collection['stages']=collection['stages'][:4]
                probe=collection['stages'][3]['probes'][0]
                probe.update(complete=False,error='RTT probe timed out')
                write_json(root/'collection.json',collection)
                traffic=json.loads((root/'traffic/traffic.json').read_text());traffic['stages']=collection['stages']
                write_json(root/'traffic/traffic.json',traffic)
                for file in (root/'traffic').glob('[0-9][0-9]-h*-h*.json'):
                    if int(file.name[:2])>=4:file.unlink()
                status=json.loads((root/'trial-status.json').read_text());status['collector_exit_code']=1
                write_json(root/'trial-status.json',status)
                with self.assertRaisesRegex(ValueError,'Actual stages differ from recorded plan'):
                    a.b.load_run(root,'baseline' if index==1 else 'predictive_ml',flow_pattern='[0-9][0-9]-h*-h*.json')
                result=a.validate_trial(root)
                self.assertIsNone(result['summary'])
                self.assertTrue(any('expected 9, recorded 4' in e for e in result['errors']))
                self.assertTrue(any('Incomplete RTT measurement' in e for e in result['errors']))
                self.assertTrue(any('Stage 3 h1-h2: RTT probe timed out' in e for e in result['errors']))

    def test_complete_stage_projection_accepts_actual_timestamps_but_rejects_real_plan_changes(self):
        from phase6c import make_four_switch_plan
        for index in (1,2):
            with self.subTest(mode=index),tempfile.TemporaryDirectory() as directory:
                plan=make_four_switch_plan(profile='feasibility');root=fixture(Path(directory),index,plan)
                file=root/'traffic/traffic.json';traffic=json.loads(file.read_text())
                for stage in traffic['stages']:
                    stage['actual_elapsed_seconds']=stage['planned_elapsed_seconds']+1.002142088
                    stage['duration_seconds']=stage['seconds']
                write_json(file,traffic)
                mode='baseline' if index==1 else 'predictive_ml'
                loaded=a.b.load_run(root,mode,flow_pattern='[0-9][0-9]-h*-h*.json')
                self.assertEqual(len(loaded['stages']),9)
                self.assertEqual(loaded['stages'][0]['actual_elapsed_seconds'],1.002142088)
                for field,value in (('planned_elapsed_seconds',999),('reverse',True),('scenario','wrong')):
                    changed=copy.deepcopy(traffic);changed['stages'][0][field]=value;write_json(file,changed)
                    with self.subTest(field=field),self.assertRaisesRegex(ValueError,'Actual stages differ from recorded plan'):
                        a.b.load_run(root,mode,flow_pattern='[0-9][0-9]-h*-h*.json')

    def test_genuine_duration_rate_endpoint_and_seed_changes_are_not_ignored(self):
        from phase6c import make_four_switch_plan
        for index in (1,2):
            with self.subTest(mode=index),tempfile.TemporaryDirectory() as directory:
                root=fixture(Path(directory),index,make_four_switch_plan(profile='feasibility'))
                file=root/'traffic/traffic.json';original=json.loads(file.read_text())
                for field in ('seconds','rate_mbps','endpoint','seed'):
                    changed=copy.deepcopy(original)
                    if field=='seed':changed['seed']+=1
                    elif field=='endpoint':changed['stages'][0]['flows'][0]['source']='h3'
                    else:changed['stages'][0][field]+=1
                    write_json(file,changed)
                    with self.subTest(field=field),self.assertRaises(ValueError):
                        a.b.load_run(root,'baseline' if index==1 else 'predictive_ml',flow_pattern='[0-9][0-9]-h*-h*.json')
