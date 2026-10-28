"""Portable, read-only views of strictly validated experiment evidence."""
from bisect import bisect_left
from datetime import datetime,timezone
import importlib.util
import json
from pathlib import Path
import statistics

from phase6c import ORDER,digest,forwarding_proof
from routing_state import routing_json

ROOT=Path(__file__).resolve().parent
METRICS={
    'average_ms':('Average RTT','ms',True),
    'p95_ms':('P95 RTT','ms',True),
    'icmp_loss_percent':('ICMP loss','%',True),
    'wall_aggregate_goodput_mbps':('Aggregate UDP goodput','Mbps',False),
    'loss_percent':('UDP loss','%',True),
    'congestion_link_seconds':('Congestion exposure','link-seconds',True),
}


def analysis():
    spec=importlib.util.spec_from_file_location('dashboard_phase6c',ROOT/'docs/phase6c/analyze.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def route_proof(route,source):
    return forwarding_proof(route,source)


def snapshot_view(snapshot):
    topology=snapshot['topology'];links=[]
    for link in topology['links']:
        prediction=link.get('prediction') or {};observation=link.get('measurement') or {}
        links.append(dict(id=link['link_id'],source=link['source_dpid'],target=link['destination_dpid'],
            active=link['active'],capacity_mbps=link['capacity_mbps'],base_delay_ms=link['base_delay_ms'],
            routing_weight=link.get('routing_weight'),current_utilization=observation.get('utilization') if observation.get('valid') else None,
            predicted_utilization=prediction.get('predicted_utilization') if prediction.get('status')=='valid' else None,
            target_timestamp=prediction.get('target_timestamp'),observation_timestamp=observation.get('observation_timestamp')))
    for link in links:
        link['congested']=link['current_utilization'] is not None and link['current_utilization']>=.8
        link['congestion_predicted']=link['predicted_utilization'] is not None and link['predicted_utilization']>=.8
    return routing_json(dict(timestamp=snapshot['timestamp'],topology_epoch=snapshot['topology_epoch'],
        policy=snapshot.get('policy',{}),routes=snapshot['routes'],topology=dict(links=links,
            switches=topology.get('switches',[]),hosts=topology.get('hosts',[]),
            nodes=[dict(id=i,name=f's{i}',host=f'h{i}',observed=i in topology['connected_switches']) for i in range(1,7)])))


def compact_snapshots(snapshots):
    """Keep every route/topology transition and first positive proof, plus 5s samples."""
    kept=[];previous=None;last_time=float('-inf')
    for raw in snapshots:
        snap=snapshot_view(raw)
        signature=digest(dict(epoch=snap['topology_epoch'],
            links=[(l['id'],l['active']) for l in snap['topology']['links']],
            routes=[(r['route_id'],r.get('installed_route'),r.get('recommended_route',{}).get('path') if r.get('recommended_route') else None,
                     route_proof(r,int(r['source_mac'].split(':')[-1],16))) for r in snap['routes']]))
        time=datetime.fromisoformat(snap['timestamp'].replace('Z','+00:00')).timestamp()
        if signature!=previous or time-last_time>=5:
            kept.append(snap);last_time=time;previous=signature
    return kept


def recovery_window(snapshots,events):
    """Require a single-link loss followed by restoration before network teardown."""
    for i in range(1,len(snapshots)):
        before,down=snapshots[i-1],snapshots[i]
        active=lambda s:{l['link_id'] for l in s['topology']['links'] if l['active']}
        failed=active(before)-active(down)
        if len(active(before))!=7 or len(active(down))!=6 or len(failed)!=1:continue
        if len(down['topology']['connected_switches'])!=6:continue
        for j in range(i+1,len(snapshots)):
            if len(snapshots[j]['topology']['connected_switches'])!=6 or len(active(snapshots[j]))<6:break
            if active(snapshots[j])==active(before):
                # Include the last good state, invalidation/install snapshots,
                # and subsequent restored state. Timestamps stay untouched.
                end=j
                restored_time=datetime.fromisoformat(snapshots[j]['timestamp'].replace('Z','+00:00')).timestamp()
                while end+1<len(snapshots):
                    following=snapshots[end+1]
                    elapsed=datetime.fromisoformat(following['timestamp'].replace('Z','+00:00')).timestamp()-restored_time
                    if (elapsed>10 or len(following['topology']['connected_switches'])!=6 or active(following)!=active(before) or
                        any(h.get('active') is False for h in following['topology'].get('hosts',[]))):break
                    end+=1
                selected=snapshots[i-1:end+1]
                start_time,end_time=selected[0]['timestamp'],selected[-1]['timestamp']
                affected={r['route_id']:r['installed_route']['path'] for r in before['routes'] if r.get('installed_route') and
                    any(f's{min(u,v)}-s{max(u,v)}' in failed for u,v in zip(r['installed_route']['path'],r['installed_route']['path'][1:]))}
                proven=[]
                for recorded in snapshots[i:j]:
                    for route in recorded['routes']:
                        installed=route.get('installed_route')
                        if (route['route_id'] in affected and installed and installed['path']!=affected[route['route_id']] and
                            route_proof(route,int(route['source_mac'].split(':')[-1],16))):
                            proven.append(dict(timestamp=recorded['timestamp'],route_id=route['route_id'],cookie=installed['cookie'],path=installed['path']))
                return dict(status='available',failed_links=sorted(failed),
                    verified_recovery_changes=proven,
                    down_timestamp=down['timestamp'],restored_timestamp=snapshots[j]['timestamp'],
                    snapshots=[snapshot_view(s) for s in selected],
                    events=[e for e in events if start_time<=e['timestamp']<=end_time],
                    note='Recorded controller snapshots and events; rule readback and packet-counter evidence are shown separately.')
    return dict(status='unavailable',reason='No complete recorded link-down and restoration sequence was found. Startup and teardown are not recovery demonstrations.',snapshots=[],events=[])


def build_package(experiment,recovery=None):
    a=analysis();root=Path(experiment).resolve()
    if (root/'trials/experiment.json').is_file():root=root/'trials'
    before=a.b.inventory(root);manifest=json.loads((root/'experiment.json').read_text())
    if manifest.get('trial_count')!=4 or manifest.get('order')!=list(ORDER):raise ValueError('A completed four-trial ABBA experiment is required')
    trials=[];identities=[]
    for i,mode in enumerate(ORDER,1):
        trial=root/f'{i:02d}-{mode}';record=json.loads((trial/'trial.json').read_text())
        status=json.loads((trial/'trial-status.json').read_text())
        if status.get('status')!='passed':raise ValueError(f'Trial {i} must have passed; historical statuses are never edited')
        if record['traffic_configuration'].get('profile')!='six-switch':raise ValueError('The original six-switch benchmark is required')
        if (record['index']!=i or record['routing_mode']!=mode or any(record[k]!=manifest[k] for k in ('source_sha256','model_sha256','plan_hash'))):
            raise ValueError('Trial provenance differs from the experiment')
        result=a.validate_trial(trial)
        if result['errors']:raise ValueError(f'Trial {i}: '+ '; '.join(result['errors']))
        summary=result['summary'];identities.append(digest(summary['comparability']))
        metrics={k:summary['rtt'][k] if k in ('average_ms','p95_ms') else summary[k] for k in METRICS}
        with a.b.readonly_database(trial/'telemetry.sqlite') as connection:
            snapshots=compact_snapshots(list(a.b.rows(connection,'routing_snapshots')))
            events=list(a.b.rows(connection,'routing_events'))
            observations=list(a.b.rows(connection,'observations'));predictions=list(a.b.rows(connection,'predictions'))
        start=min(s['start'] for s in summary['traffic_stages']);end=max(s['end'] for s in summary['traffic_stages'])
        snapshots=[s for s in snapshots if start<=a.b.timestamp(s['timestamp'])<=end]
        times=[a.b.timestamp(s['timestamp']) for s in snapshots]
        changes={e['cookie']:e for e in summary['route_changes']}
        for event in events:
            change=changes.get(event.get('cookie')) if event['kind']=='route_activated' else None
            if change:event.update(forwarding_evidence=change['forwarding_evidence'])
            time=a.b.timestamp((event.get('forwarding_evidence') or {}).get('timestamp',event['timestamp']))
            index=bisect_left(times,time)
            event['snapshot_index']=index if index<len(times) and times[index]-time<=5 else None
        forecast={(p['link_id'],p['observation_timestamp']):p for p in predictions}
        history=[]
        for row in observations:
            if row['link_id']!='s2-s5' or not start<=a.b.timestamp(row['observation_timestamp'])<=end:continue
            pred=forecast.get(('s2-s5',row['observation_timestamp']),{})
            history.append(dict(timestamp=row['observation_timestamp'],current=row.get('utilization') if row.get('valid') else None,
                predicted=pred.get('predicted_utilization') if pred.get('status')=='valid' else None,
                target_timestamp=pred.get('target_timestamp')))
        trials.append(dict(index=i,mode=mode,trial_id=record['trial_id'],status='passed',metrics=metrics,
            routing=summary['routing'],snapshots=snapshots,events=routing_json([e for e in events if start<=a.b.timestamp(e['timestamp'])<=end]),
            backbone_history=history,stages=summary['traffic_stages'],rtt_stages=summary['stages'],
            verified_traffic_changes=routing_json([e for e in summary['route_changes'] if e.get('forwarding_evidence')]),
            source_sha256=record['source_sha256'],model_sha256=record['model_sha256']))
    if len(set(identities))!=1:raise ValueError('Trial topology, model, platform or routing policy differ')
    comparison={}
    for key,(label,unit,lower) in METRICS.items():
        values={mode:statistics.mean(t['metrics'][key] for t in trials if t['mode']==mode) for mode in set(ORDER)}
        change=(values['predictive_ml']-values['baseline'])/values['baseline']*100 if values['baseline'] else None
        comparison[key]=dict(label=label,unit=unit,lower_is_better=lower,change_percent=change,**values)
    recovery_data=dict(status='unavailable',reason='No recovery recording was supplied.',snapshots=[],events=[])
    recovery_inputs={}
    if recovery:
        recovery=Path(recovery).resolve();recovery_inputs=a.b.inventory(recovery)
        with a.b.readonly_database(recovery/'telemetry.sqlite') as connection:
            recovery_data=recovery_window(list(a.b.rows(connection,'routing_snapshots')),list(a.b.rows(connection,'routing_events')))
        acceptance=recovery/'acceptance.json'
        if acceptance.is_file():
            record=json.loads(acceptance.read_text())
            recovery_data['acceptance']=dict(status=record.get('status'),backbone_failure_recovery=record.get('backbone_failure_recovery'),
                final_pingall_loss=record.get('final_pingall_loss'))
        recovery_data=routing_json(dict(recovery_data,source_directory=recovery.name))
        if a.b.inventory(recovery)!=recovery_inputs:raise ValueError('Recovery evidence changed during preparation')
    if a.b.inventory(root)!=before:raise ValueError('Experiment evidence changed during preparation')
    payload=dict(schema_version=1,status='available',experiment=root.parent.name if root.name=='trials' else root.name,
        prepared_at=datetime.now(timezone.utc).isoformat(),comparison=comparison,trials=trials,recovery=recovery_data,
        provenance=dict(source_sha256=manifest['source_sha256'],model_sha256=manifest['model_sha256'],
            plan_hash=manifest['plan_hash'],input_files=before,recovery_input_files=recovery_inputs,
            validator_sha256=a.b.inventory(ROOT/'docs/phase6c')['analyze.py']['sha256']),
        note='Controlled six-switch Mininet experiment · two trials per mode · descriptive comparison, not statistical significance. Mode values are equal-weight trial means; congestion exposure can increase under ML.')
    payload['package_sha256']=digest(payload)
    return payload


def load_package(path):
    try:
        package=json.loads(Path(path).read_text());checksum=package.pop('package_sha256')
        if (checksum!=digest(package) or package.get('schema_version')!=1 or package.get('status')!='available' or
            len(package.get('trials',[]))!=4):raise ValueError('Invalid or changed portable evidence package; prepare it again')
        return dict(package,package_sha256=checksum)
    except (OSError,ValueError,KeyError,TypeError) as error:
        return dict(status='unavailable',reason=f'Benchmark data unavailable: {error}',trials=[],comparison={},
            recovery=dict(status='unavailable',reason='Prepare a verified evidence package first.',snapshots=[],events=[]))
