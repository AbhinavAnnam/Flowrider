"""Read-only Phase 6C validation and repeated-trial analysis, reusing Phase 6B."""
import argparse
import csv
import importlib.util
import json
from pathlib import Path
import re
import statistics
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from phase6c import (ORDER, PAIRS, check_queue, digest, forwarding_proof, parse_ping,
                    rtt_metrics, validate_collection, validate_plan, write_json, expected_tc,
                    bottleneck_interfaces, forecast_metrics)
spec=importlib.util.spec_from_file_location('phase6b_analysis',ROOT/'docs/phase6b/analyze.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)


def positive_dump_counters(record,path,cookie,source,destination):
    if not path or cookie is None:return []
    counters=[]
    for dpid in path:
        lines=[line for line in record.get('ovs',{}).get(f's{dpid}','').splitlines()
            if f'cookie=0x{cookie:x},' in line and f'dl_src=00:00:00:00:00:{source:02x}' in line
            and f'dl_dst=00:00:00:00:00:{destination:02x}' in line]
        if len(lines)!=1:return []
        count=re.search(r'\bn_packets=([1-9]\d*)\b',lines[0])
        if not count:return []
        counters.append(dict(dpid=dpid,source_mac=f'00:00:00:00:00:{source:02x}',packet_count=int(count[1])))
    return counters


def verified_dump(record,route,source,destination):
    installed=route.get('installed_route') or {}
    return bool(positive_dump_counters(record,installed.get('path'),installed.get('cookie'),source,destination))


def supplement_changed_route_evidence(run,collection,captures):
    # A late activation can precede the final independent dump but have its next
    # periodic controller audit after traffic ends. Use that exact version's
    # in-stage counters; tail audits cannot establish measured-stage forwarding.
    for stage in collection['stages']:
        for flow in stage['flows']:
            src,dst=flow['source'],flow['destination']
            source,destination=int(src[1:]),int(dst[1:])
            route=stage.get('forwarding',{}).get(f'{src}-{dst}',{}).get('route',{})
            if not route or b.route_key(route)!=tuple(sorted((source,destination))):continue
            for change in run['tracked_route_changes']:
                if (change['forwarding_evidence'] or change['route_id']!=route.get('route_id') or
                    change.get('verification')!='openflow_flow_stats'):continue
                for capture in captures:
                    if (capture['stage']!=stage['index'] or not
                        max(stage['traffic_started'],b.timestamp(change['timestamp']))<=b.timestamp(capture['timestamp'])<=stage['traffic_finished']):continue
                    counters=positive_dump_counters(capture,change['path'],change['cookie'],source,destination)
                    if counters:
                        change['forwarding_evidence']=dict(timestamp=capture['timestamp'],actual_source_host=source,
                            minimum_hop_packets=min(c['packet_count'] for c in counters),counters=counters,
                            evidence_source='independent_ovs_dump',stage=stage['index'])
                        break
    run['route_metrics']['tracked_changes_positive_hops']=sum(bool(c['forwarding_evidence']) for c in run['tracked_route_changes'])


def validate_trial(root,*,revalidate=False):
    root=Path(root);errors=[]
    def need(ok,message):
        if not ok:errors.append(message)
    manifest=json.loads((root/'trial.json').read_text());plan=manifest['traffic_configuration'];validate_plan(plan)
    pairs=plan['flow_pairs'];profile=plan.get('topology','six')
    bottleneck=plan['bottleneck']['link_id']
    bottleneck_nodes=set(map(int,bottleneck.replace('s','').split('-')))
    from topology_config import wired_links
    link_count=len(wired_links(profile))
    status=json.loads((root/'trial-status.json').read_text());collection=json.loads((root/'collection.json').read_text())
    need(manifest['plan_hash']==digest(plan),'Manifest plan hash mismatch')
    need(status.get('collector_exit_code')==0 and not status.get('interrupted'),'Collector failed/interrupted')
    need(not status.get('error') and (revalidate or not status.get('validation_error')),'Recorded trial failure')
    need(status.get('controller_stop_requested') and status.get('controller_exit_code') in (0,-15),
         'Unexpected controller exit or forced kill')
    if profile=='phase6c' or plan.get('profile')=='six-switch':
        need(status.get('model_unchanged') and status.get('source_unchanged'),
             'Model or benchmark sources changed during collection')
    need(collection.get('status')=='collected','Collector did not complete')
    errors.extend(validate_collection(plan,collection))
    # An aborted run is genuinely incomplete. Report its collector/probe cause
    # before the complete-run reader compares its truncated stage list.
    if collection.get('status')!='collected' or len(collection.get('stages',[]))!=len(plan['stages']):
        if collection.get('error'):errors.append('Collector error: '+collection['error'])
        return dict(errors=errors,summary=None)
    run=b.load_run(root,manifest['routing_mode'],flow_pattern='[0-9][0-9]-h*-h*.json')
    captures=[json.loads(line) for line in (root/'forwarding-tc.jsonl').read_text().splitlines()]
    supplement_changed_route_evidence(run,collection,captures)
    # Phase 6B's historical exposure denominator is seven links.
    run['utilization']['coverage_percent']=run['utilization']['coverage_link_seconds']/(link_count*(run['end']-run['start']))*100
    config=run['config']
    need(manifest['topology_version']==plan['topology_version'] and
         config.get('topology_version')==plan['topology_version'],'Topology identity differs from selected plan')
    need(config['trial_id']==manifest['trial_id'] and config['traffic_configuration']==plan,'Database trial/plan mismatch')
    need(config.get('experiment_link_capacities_mbps')==plan['link_capacities_mbps'],'Physical/telemetry capacity mismatch')
    need(run['traffic']['trial_id']==manifest['trial_id'],'Traffic trial ID mismatch')
    need(len(run['epochs'])==1,'Topology changed during trial')
    need(not run['failures'],'Failed or quarantined forwarding transition')
    critical=[line for line in (root/'controller.log').read_text().splitlines()
              if re.search(r'Traceback|\bERROR\b|\bCRITICAL\b|AttributeError:|Telemetry startup failed|'
                  r'Telemetry backlog full|Telemetry shutdown backlog|Telemetry worker disabled|'
                  r'Telemetry operation failed|ML model load failed|Routing notification failed|'
                  r'Routing history could not be queued|Predictive route operation failed|'
                  r'OpenFlow (?:telemetry request )?error|forwarding disabled|rejecting connection',line)]
    need(not critical,'Controller errors (including shutdown); inspect controller.log')
    need(run['utilization']['coverage_percent']>=95,'Telemetry coverage below 95%')
    need(len(run['utilization']['per_link'])==link_count,'Missing link telemetry')
    need(all(p['coverage_seconds']/(run['end']-run['start'])>=.95 for p in run['utilization']['per_link'].values()),
         'A link has less than 95% valid sample coverage')
    need(run['route_metrics']['path_changes']==run['route_metrics']['verified_path_changes'],'Unverified path activation')
    need(run['route_metrics']['tracked_changes']==run['route_metrics']['tracked_changes_positive_hops'],
         'Measured-pair changed route lacks positive counters in actual traffic direction')
    if manifest['routing_mode']=='baseline':
        need(run['route_metrics']['path_changes']==0 and run['route_metrics']['candidate_events']==0,
             'Baseline changed paths/initiated congestion rerouting')
    for pair in ([int(src[1:]),int(dst[1:])] for src,dst in pairs):
        initial=next((p['path'] for p in run['installed_trace'] if p['pair']==pair),[])
        need(any({u,v}==bottleneck_nodes for u,v in zip(initial,initial[1:])),
             'Initial measured-pair forwarding did not use the shared bottleneck')
    for encoded in run['topology_fingerprints']:
        links=json.loads(encoded)['links']
        expected_links={f"s{min(l['src'],l['dst'])}-s{max(l['src'],l['dst'])}":l for l in wired_links(profile)}
        need({l['link_id'] for l in links}==set(expected_links),'Snapshot link configuration has missing/extra links')
        for link in links:
            expected=expected_links.get(link['link_id'])
            need(expected is not None and link['base_delay_ms']==expected['base_delay'] and
                 link['ports']=={str(expected['src']):expected['src_port'],str(expected['dst']):expected['dst_port']},
                 'Snapshot link configuration differs from physical wiring/delays')
        need(all(l['capacity_mbps']==(plan['bottleneck']['bandwidth_mbps'] if l['link_id']==bottleneck else 100)
                 for l in links),'Snapshot capacities differ from applied plan')
    for stage in run['stages']:
        for link,points in run['link_series'].items():
            coverage=b.exposure(points,stage['start'],stage['end'])['coverage_seconds']
            need(coverage/(stage['end']-stage['start'])>=.8,
                 f'Stage {stage["index"]} {link}: less than 80% valid telemetry interval coverage')
    from topology_config import check_tc_output
    expected_interfaces=expected_tc(plan)
    for file in ('applied-tc.json','final-tc.json'):
        records=json.loads((root/file).read_text())
        need(len(records)==len(expected_interfaces) and {r['interface'] for r in records}==set(expected_interfaces),'Incomplete physical TC capture')
        for record in records:
            expected=expected_interfaces.get(record['interface'])
            if expected is None:continue
            need((record['bandwidth_mbps'],record['delay_ms'],record['queue_packets'])==expected,
                 'Live TC metadata differs from physical plan')
            check_tc_output(record['qdisc'],record['classes'],expected[0],expected[1])
            if expected[2] is not None:check_queue(record['qdisc'],expected[2])
    probe_stages=[];samples=[];sent=received=0
    for stage in collection['stages']:
        per_pair=[]
        for probe in stage['probes']:
            value=parse_ping((root/'traffic'/probe['file']).read_text(),probe['expected_count'])
            need(all(probe['started']<=s['timestamp']<=b.timestamp(stage['finished_timestamp'])+1
                     for s in value['samples']),'Probe timestamps outside recorded stage')
            per_pair.append(dict(source=probe['source'],destination=probe['destination'],**value))
            samples.extend(value['samples']);sent+=value['transmitted'];received+=value['received']
        stage_captures=[r for r in captures if r['stage']==stage['index'] and
            stage['traffic_started']<=b.timestamp(r['timestamp'])<=stage['traffic_finished']]
        for src,dst in pairs:
            proof=stage.get('forwarding',{}).get(f'{src}-{dst}',{})
            route=proof.get('route',{})
            need(forwarding_proof(route,int(src[1:])),'Missing saved exact positive per-hop readback')
            initial=proof.get('starting_counters',{});final=proof.get('ending_counters',{})
            need(bool(initial) and initial.keys()==final.keys() and
                 set(initial)=={str(d) for d in route.get('installed_route',{}).get('path',[])} and
                 all(final[k]>v for k,v in initial.items()),
                 'Missing measured-stage per-hop counter increases')
            need(any(verified_dump(c,route,int(src[1:]),int(dst[1:])) for c in stage_captures),
                 f'Stage {stage["index"]} {src}-{dst}: missing independent positive OVS rules')
            if profile=='phase6c' or plan.get('profile')=='six-switch':
                reverse=stage.get('bidirectional_forwarding',{}).get(f'{src}-{dst}',{})
                reverse_route=reverse.get('route',{})
                before=reverse.get('starting_counters',{});after=reverse.get('ending_counters',{})
                need(forwarding_proof(reverse_route,int(dst[1:])) and bool(before) and
                     before.keys()==after.keys() and
                     set(before)=={str(d) for d in reverse_route.get('installed_route',{}).get('path',[])} and
                     all(after[k]>v for k,v in before.items()),'Missing reverse per-hop counter increases')
                need(bool(reverse_route) and any(verified_dump(c,reverse_route,int(dst[1:]),int(src[1:]))
                     for c in stage_captures),'Missing independent reverse OVS forwarding proof')
        probe_stages.append(dict(index=stage['index'],scenario=stage['scenario'],pairs=per_pair,
            **rtt_metrics([s for pair in per_pair for s in pair['samples']]),
            loss_percent=100*(sum(p['transmitted']-p['received'] for p in per_pair))/sum(p['transmitted'] for p in per_pair)))
    sustained=[s for s in run['stages'] if s['scenario']=='sustained']
    sustained_captures=[c for c in captures if c['stage'] in [s['index'] for s in sustained]]
    queue_pressure=any(re.search(r'\bbacklog [1-9][0-9.]*[KMG]?b',q)
        for c in sustained_captures for q in c['tc'].values())
    for interface in bottleneck_interfaces(plan):
        drops=[sum(map(int,re.findall(r'\bdropped (\d+)',c['tc'].get(interface,'')))) for c in sustained_captures]
        queue_pressure=queue_pressure or (len(drops)>1 and drops[-1]>drops[0])
    sustained_observations=[p for p in run['link_series'][bottleneck] if p['valid'] and p['utilization']>=.8
        and any(s['start']<=p['time']<=s['end'] for s in sustained)]
    if manifest['routing_mode']=='baseline':
        need(queue_pressure and len(sustained_observations)>=2,'Baseline bottleneck congestion not demonstrated by TC and telemetry')
    # A successful predictive avoidance must not be rejected for avoiding a queue.
    need(bool(run['model_versions']) and run['prediction_statuses'].get('valid',0)>0,'No fresh model inference recorded')
    with b.readonly_database(root/'telemetry.sqlite') as connection:
        events=[e for e in b.rows(connection,'routing_events') if run['start']<=b.timestamp(e['timestamp'])<=run['end']]
        observations=list(b.rows(connection,'observations'));predictions=list(b.rows(connection,'predictions'))
        directions=list(b.rows(connection,'directions')) if plan.get('profile')=='six-switch' else []
    window_predictions=[p for p in predictions if run['start']<=b.timestamp(p['observation_timestamp'])<=run['end']]
    accuracy=forecast_metrics(observations,window_predictions)
    # Reuse the established timestamp/valid-history evaluation on real schema
    # records. Minimal arithmetic fixtures do not impersonate that provenance.
    if observations and all('schema_version' in row for row in observations):
        from phase3 import evaluate_predictions
        try:accuracy['existing_phase3_evaluation']=evaluate_predictions(observations,window_predictions)
        except (ValueError,KeyError,TypeError) as error:
            accuracy['existing_phase3_evaluation_status']=str(error)
            need(False,'No matured valid-history forecast targets available')
    need(not any(e['kind'] in ('installation_rejected','packet_release_failed') for e in events),
         'Rejected installation or failed packet release')
    accepted=[e for e in events if e['kind']=='installation_started' and e.get('reason')=='predicted_congestion']
    spare={link:b.exposure(points,run['start'],run['end']) for link,points in run['link_series'].items()
           if link in ('s1-s3','s3-s4')}
    if profile=='phase6c':
        need(len(spare)==2 and all(v['peak_utilization'] is not None and v['peak_utilization']<.8
            for v in spare.values()),'Alternative path spare capacity not demonstrated')
    if plan.get('profile')=='six-switch' and manifest['routing_mode']=='baseline':
        # Check directional spare capacity: opposite directions are independent.
        # Shared edges already carry this flow, so rerouting adds no load there.
        for (src,dst),path,rate in zip(((1,4),(2,5),(3,6)),
                ([1,6,5,4],[2,3,4,5],[3,4,5,6]),plan['flow_rates_mbps']['sustained']):
            baseline=next((p['path'] for p in run['installed_trace'] if p['pair']==[src,dst]),[])
            shared=set(zip(baseline,baseline[1:]))
            for u,v in zip(path,path[1:]):
                direction_samples=[r for r in directions if r.get('valid') and r['source_dpid']==u and r['destination_dpid']==v
                         and any(s['start']<=b.timestamp(r['observation_timestamp'])<=s['end'] for s in sustained)]
                added=0 if (u,v) in shared else rate*1.03  # payload plus wire overhead margin
                need(bool(direction_samples) and max(r['tx_mbps'] for r in direction_samples)+added<=min(r['capacity_mbps'] for r in direction_samples),
                     f'Baseline alternative h{src}-h{dst}: no demonstrated spare capacity on s{u}->s{v}')
    queue_drops={}
    for interface in bottleneck_interfaces(plan):
        points=[]
        for capture in captures:
            text=capture['tc'].get(interface,'')
            # netem owns the bounded packet queue; do not double-count parent HTB drops.
            blocks=re.split(r'(?=qdisc )',text)
            block=next((v for v in blocks if v.startswith('qdisc netem ')),text)
            match=re.search(r'\bdropped (\d+)',block)
            if match:points.append(dict(timestamp=capture['timestamp'],stage=capture['stage'],drops=int(match[1])))
        queue_drops[interface]=dict(samples=points,delta=(points[-1]['drops']-points[0]['drops']) if points else None)
    summary=dict(trial_id=manifest['trial_id'],routing_mode=manifest['routing_mode'],index=manifest['index'],
        rtt=rtt_metrics(samples),icmp_transmitted=sent,icmp_received=received,icmp_loss_percent=(sent-received)/sent*100,
        **run['metrics'],congestion_link_seconds=run['utilization']['congestion_link_seconds'],
        telemetry_coverage_percent=run['utilization']['coverage_percent'],
        routing=dict(**run['route_metrics'],accepted_transition_attempts=len(accepted)),
        queue_drops=queue_drops,alternative_path_utilization=spare,
        sustained_backbone_congested_samples=len(sustained_observations),tc_queue_pressure_observed=queue_pressure,
        flow_pairs=pairs,forecast_accuracy=accuracy,
        utilization=run['utilization'],link_series=run['link_series'],
        stages=probe_stages,traffic_stages=run['stages'],route_changes=run['route_changes'],
        recommended_changes=run['recommendations'],accepted_transitions=accepted,installed_trace=run['installed_trace'])
    summary['comparability']=dict(topology=run['topology_fingerprints'],policy=run['policy_fingerprints'],
        model_versions=run['model_versions'],model_path=config.get('model_path'),
        flow_platform=sorted({(f['version'],f['system']) for f in run['flows']}))
    return dict(errors=errors,summary=summary)


def aggregate(summaries):
    result={}
    metrics=('average_ms','median_ms','p95_ms','maximum_ms','icmp_loss_percent','sender_mbps',
             'receiver_mbps','wall_aggregate_goodput_mbps','loss_percent','congestion_link_seconds')
    for mode in ('baseline','predictive_ml'):
        trials=[s for s in summaries if s['routing_mode']==mode];group={}
        for key in metrics:
            values=[s['rtt'][key] if key.endswith('_ms') else s[key] for s in trials]
            values=[v for v in values if v is not None]
            group[key]=dict(n=len(values),mean=statistics.mean(values) if values else None,
                sample_sd=statistics.stdev(values) if len(values)>1 else None,
                minimum=min(values,default=None),maximum=max(values,default=None))
        result[mode]=dict(trial_count=len(trials),metrics=group)
    return result


def figures(summaries,output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    colors={'baseline':'#187b9d','predictive_ml':'#d66a28'}
    for metric,ylabel in [('average_ms','RTT average (ms)'),('p95_ms','RTT P95 (ms)'),
                         ('loss_percent','ICMP loss (%)'),('udp_loss','UDP loss (%)'),
                         ('goodput','Aggregate goodput (Mbps)')]:
        fig,ax=plt.subplots(figsize=(9,4))
        for trial in summaries:
            stages=trial['stages']
            values=([s['metrics']['received_bytes']*8/(s['end']-s['start'])/1e6 for s in trial['traffic_stages']]
                    if metric=='goodput' else [s['metrics']['loss_percent'] for s in trial['traffic_stages']]
                    if metric=='udp_loss' else [s[metric] for s in stages])
            ax.plot([s['index'] for s in stages],values,'o-',label=trial['trial_id'],
                    color=colors[trial['routing_mode']],alpha=.8)
        ax.set(xlabel='Matched traffic stage',ylabel=ylabel);ax.legend(fontsize=7);ax.grid(alpha=.2)
        fig.tight_layout();fig.savefig(output/f'{metric}.png');plt.close(fig)
    fig,ax=plt.subplots(figsize=(9,4))
    ax.bar([s['index'] for s in summaries],[s['congestion_link_seconds'] for s in summaries],
           color=[colors[s['routing_mode']] for s in summaries])
    ax.set(xlabel='ABBA trial index',ylabel='Congestion exposure ≥80% (link-seconds)')
    fig.tight_layout();fig.savefig(output/'congestion.png');plt.close(fig)
    fig,ax=plt.subplots(figsize=(10,4))
    for trial in summaries:
        for pair in ([int(src[1:]),int(dst[1:])] for src,dst in trial.get('flow_pairs',PAIRS)):
            # Route IDs are controller-defined: installed_trace carries explicit pairs.
            points=[p for p in trial['installed_trace'] if p['pair']==pair]
            for point in points:
                ax.scatter(point['time_seconds'],trial['index'],color=colors[trial['routing_mode']],marker='|',s=150)
                ax.annotate(f'h{pair[0]}-h{pair[1]} '+','.join(map(str,point['path'])),
                            (point['time_seconds'],trial['index']),fontsize=6,rotation=25)
    ax.set(xlabel='Actual seconds since traffic start',ylabel='ABBA trial',yticks=[1,2,3,4],
           title='Installed path records; exact activations and counter proofs are in results.json')
    fig.tight_layout();fig.savefig(output/'routing.png');plt.close(fig)
    fig,axes=plt.subplots(len(summaries),1,figsize=(10,3*len(summaries)),squeeze=False)
    for row,trial in zip(axes,summaries):
        ax=row[0]
        for link,points in trial.get('link_series',{}).items():
            ax.plot([p['time']-trial['traffic_stages'][0]['start'] for p in points if p['valid']],
                    [p['utilization'] for p in points if p['valid']],label=link)
        ax.axhline(.8,color='gray',linestyle='--');ax.set(title=trial['trial_id'],ylabel='Link utilization')
        ax.legend(fontsize=7)
    axes[-1][0].set_xlabel('Seconds since traffic start')
    fig.tight_layout();fig.savefig(output/'utilization.png');plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--validate-only',action='store_true',help='Recheck one stopped trial read-only; print verdict without reports or status updates')
    args=parser.parse_args();args.experiment=args.experiment.resolve()
    if args.validate_only:
        if args.output is not None:parser.error('--validate-only does not write output')
        before=b.inventory(args.experiment)
        prior=json.loads((args.experiment/'trial-status.json').read_text())
        result=validate_trial(args.experiment,revalidate=True)
        if b.inventory(args.experiment)!=before:raise RuntimeError('Raw evidence changed during revalidation')
        if prior.get('validation_error'):print('Historical validation error (retained): '+prior['validation_error'])
        print(('FAIL' if result['errors'] else 'PASS')+': '+str(args.experiment))
        for error in result['errors']:print(error)
        if result['summary']:
            routing=result['summary']['routing']
            print(f"Measured-pair changed versions with positive counters: {routing['tracked_changes_positive_hops']}/{routing['tracked_changes']}")
        if result['errors']:raise SystemExit(1)
        return
    if args.output is None:parser.error('--output is required unless --validate-only is used')
    args.output=args.output.resolve()
    if args.output==args.experiment or args.experiment in args.output.parents:
        parser.error('Analysis output must be outside raw experiment evidence')
    before=b.inventory(args.experiment)
    experiment=json.loads((args.experiment/'experiment.json').read_text())
    summaries=[];invalid=[];identities=[]
    count=experiment.get('trial_count',4)
    if count not in (2,4):raise ValueError('Expected a paired feasibility run or complete ABBA')
    for index,mode in enumerate(ORDER[:count],1):
        root=args.experiment/f'{index:02d}-{mode}'
        try:
            manifest=json.loads((root/'trial.json').read_text())
            if json.loads((root/'trial-status.json').read_text()).get('status')!='passed':
                raise ValueError('Trial did not finish with passed status')
            if (manifest['plan_hash']!=experiment['plan_hash'] or manifest['model_sha256']!=experiment['model_sha256']
                or manifest['source_sha256']!=experiment['source_sha256'] or manifest['index']!=index
                or manifest['routing_mode']!=mode):raise ValueError('Counterbalance/model/source/plan mismatch')
            result=validate_trial(root)
            if result['errors']:raise ValueError('; '.join(result['errors']))
            identities.append(manifest['trial_id']);summaries.append(result['summary'])
        except Exception as error:invalid.append(dict(trial=index,error=str(error)))
    if len(identities)!=len(set(identities)):invalid.append(dict(error='Duplicate trial identity'))
    if summaries and len({digest(s['comparability']) for s in summaries})!=1:
        invalid.append(dict(error='Live topology, routing policy, model versions/path or iperf platform differ'))
    after=b.inventory(args.experiment)
    if before!=after:raise RuntimeError('Raw evidence changed during analysis; stop all trial processes first')
    args.output.mkdir(parents=True)  # no overwrite of prior analyses either
    modes=aggregate(summaries)
    write_json(args.output/'input-manifest.json',before)
    write_json(args.output/'results.json',dict(status='complete' if not invalid else 'incomplete',
        invalid=invalid,trials=summaries,per_mode=modes))
    with (args.output/'routing-timeline.csv').open('w',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=['trial_id','kind','seconds','route_id','path','cookie','positive_hop_proof'])
        writer.writeheader()
        for trial in summaries:
            start=trial['traffic_stages'][0]['start']
            for kind,records in [('recommendation',trial['recommended_changes']),
                                 ('accepted_attempt',trial['accepted_transitions']),('verified_activation',trial['route_changes'])]:
                for record in records:
                    writer.writerow(dict(trial_id=trial['trial_id'],kind=kind,
                        seconds=record.get('time_seconds',b.timestamp(record['timestamp'])-start if 'timestamp' in record else None),
                        route_id=record['route_id'],path=json.dumps(record['path']),cookie=record.get('cookie'),
                        positive_hop_proof=bool(record.get('forwarding_evidence'))))
    lines=['# Phase 6C controlled experiment results','',
        'Status: '+(('COMPLETE ABBA' if count==4 else 'COMPLETE PAIRED FEASIBILITY; no statistical conclusion')
                    if not invalid else 'INCOMPLETE / INVALID; no comparative conclusion'),'',
        '| Trial | Mode | RTT avg / P95 ms | ICMP loss % | Receiver Mbps per flow | ≥80% link-seconds | Verified changes / traffic pair |',
        '| --- | --- | --- | --- | --- | --- | --- |']
    for trial in summaries:
        lines.append(f"| {trial['index']} | {trial['routing_mode']} | {trial['rtt']['average_ms']} / {trial['rtt']['p95_ms']} | "
            f"{trial['icmp_loss_percent']:.4f} | {trial['receiver_mbps']:.4f} | {trial['congestion_link_seconds']:.3f} | "
            f"{trial['routing']['verified_path_changes']} / {trial['routing']['tracked_changes_positive_hops']} |")
    lines+=['','Per-mode mean ± sample SD across trial-level metrics (each trial has equal weight):','',
        '| Metric | Baseline mean ± SD | ML mean ± SD | ML − baseline | Change % |',
        '| --- | --- | --- | --- | --- |']
    for key in modes['baseline']['metrics']:
        a=modes['baseline']['metrics'][key];c=modes['predictive_ml']['metrics'][key]
        difference=b.delta(a['mean'],c['mean']) if a['mean'] is not None and c['mean'] is not None and not invalid else None
        lines.append(f"| {key} | {a['mean']} ± {a['sample_sd']} (n={a['n']}) | {c['mean']} ± {c['sample_sd']} (n={c['n']}) | "
                     f"{difference['absolute'] if difference else 'unavailable'} | {difference['percent'] if difference else 'unavailable'} |")
    lines+=['','Matched scenario measurements (low is the low-congestion control):','',
        '| Trial | Scenario | Average / P95 / max RTT ms | ICMP loss % | UDP receiver Mbps per flow | UDP loss % |',
        '| --- | --- | --- | --- | --- | --- |']
    for trial in summaries:
        for scenario in dict.fromkeys(s['scenario'] for s in trial['stages']):
            stages=[s for s in trial['stages'] if s['scenario']==scenario]
            probes=[p for s in stages for p in s['pairs']]
            rtt=rtt_metrics([v for p in probes for v in p['samples']])
            sent=sum(p['transmitted'] for p in probes);received=sum(p['received'] for p in probes)
            udp=b.aggregate_flows([s['metrics'] for s in trial['traffic_stages'] if s['scenario']==scenario])
            lines.append(f"| {trial['index']} | {scenario} | {rtt['average_ms']} / {rtt['p95_ms']} / {rtt['maximum_ms']} | "
                f"{100*(sent-received)/sent:.4f} | {udp['receiver_mbps']:.4f} | {udp['loss_percent']} |")
    lines+=['','Forecast accuracy, queue drops and route evidence:']
    for trial in summaries:
        accuracy=trial.get('forecast_accuracy',{})
        drops={k:v['delta'] for k,v in trial.get('queue_drops',{}).items()}
        lines.append(f"- Trial {trial['index']} ({trial['routing_mode']}): forecast matches={accuracy.get('matched_count')}, "
            f"MAE={accuracy.get('mae')}, RMSE={accuracy.get('rmse')}, observed advance-warning lead seconds="
            f"{accuracy.get('lead_times_seconds')}; netem drop deltas={drops}; "
            f"ML-triggered verified changes={trial['routing']['predictive_changes']}. "
            'An empty lead list means no matched advance warning was established.')
    lines+=['','RTT P95 uses linear interpolation of received ping samples; lost replies have no inferred RTT. '
        'Loss uses transmitted minus received counts. UDP goodput/loss and sampled congestion exposure reuse Phase 6B definitions. '
        'Recommendations, accepted installation attempts and verified activations are distinct in results.json. '
        'Same-path refreshes are excluded from route changes. No hypothesis test or statistical significance is claimed. '
        'The ABBA run has two trials per mode; feasibility has one per mode. '
        'These describe observations only; ICMP priority and five-second telemetry limit interpretation.']
    if not invalid:
        lines+=['','Observed direction of differences (descriptive means; no significance test):']
        for key,label,lower_is_better in [('average_ms','mean RTT',True),('p95_ms','P95 RTT',True),
                ('icmp_loss_percent','ICMP loss',True),('loss_percent','UDP loss',True),
                ('wall_aggregate_goodput_mbps','aggregate goodput',False),('congestion_link_seconds','congestion exposure',True)]:
            x=modes['baseline']['metrics'][key]['mean'];y=modes['predictive_ml']['metrics'][key]['mean']
            direction=('unavailable' if x is None or y is None else 'equal' if x==y else
                       'lower' if y<x else 'higher')
            assessment=('unavailable' if direction=='unavailable' else 'no difference' if direction=='equal' else
                        'favorable' if (y<x)==lower_is_better else 'unfavorable')
            lines.append(f'- Predictive {label}: {direction} ({assessment}); this does not establish causation or significance.')
    if invalid:lines+=['','Validation failures:',json.dumps(invalid,indent=2)]
    (args.output/'report.md').write_text('\n'.join(lines)+'\n')
    if summaries:figures(summaries,args.output)
    if invalid:raise SystemExit(1)


if __name__=='__main__':main()
