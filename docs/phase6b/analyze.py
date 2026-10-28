"""Read-only paired Phase 6B analysis. Run from the repository root; no controller imports."""
import argparse
from collections import Counter,defaultdict
import csv
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from contextlib import contextmanager
import shutil
import tempfile


@contextmanager
def readonly_database(path):
    """Inspect stopped trials without creating WAL/SHM files in raw evidence."""
    path=Path(path).resolve();wal=Path(str(path)+'-wal')
    with tempfile.TemporaryDirectory(prefix='flowrider-analysis-') as directory:
        if wal.exists() and wal.stat().st_size:
            # immutable=1 ignores a nonempty WAL. Inspect a private DB+WAL copy
            # instead, so committed samples are retained and SHM stays private.
            private=Path(directory)/path.name
            shutil.copyfile(path,private);shutil.copyfile(wal,Path(str(private)+'-wal'))
            uri=f'{private.as_uri()}?mode=ro'
        else:uri=f'{path.as_uri()}?mode=ro&immutable=1'
        connection=sqlite3.connect(uri,uri=True)
        try:yield connection
        finally:connection.close()


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


def delta(baseline,predictive):
    return dict(absolute=predictive-baseline,percent=(predictive-baseline)/baseline*100 if baseline else None)


def flow_metrics(data):
    if data.get('error'):raise ValueError(f"iperf error: {data['error']}")
    end=data.get('end',{});sender=end.get('sum_sent');receiver=end.get('sum_received')
    if not receiver or not all(k in receiver for k in ('bytes','seconds','lost_packets','packets')):
        raise ValueError('Missing complete receiver summary')
    if not sender or not all(k in sender for k in ('bytes','seconds')):raise ValueError('Missing sender summary')
    if sender['seconds']<=0 or receiver['seconds']<=0:raise ValueError('Invalid measurement duration')
    if receiver['lost_packets']<0:raise ValueError('Negative receiver loss cannot be treated as zero loss')
    if data['start']['test_start']['protocol']!='UDP':raise ValueError('Expected UDP')
    return dict(sent_bytes=sender['bytes'],received_bytes=receiver['bytes'],sender_seconds=sender['seconds'],
        receiver_seconds=receiver['seconds'],lost_packets=receiver['lost_packets'],packets=receiver['packets'])


def aggregate_flows(rows):
    sums={k:sum(r[k] for r in rows) for k in ('sent_bytes','received_bytes','sender_seconds','receiver_seconds','lost_packets','packets')}
    return dict(**sums,sender_mbps=sums['sent_bytes']*8/sums['sender_seconds']/1e6,
        receiver_mbps=sums['received_bytes']*8/sums['receiver_seconds']/1e6,
        loss_percent=100*sums['lost_packets']/sums['packets'] if sums['packets'] else None)


def exposure(rows,start,end):
    """Interval averages describe preceding intervals; never bridge invalid/large gaps."""
    covered=integral=congested=excess=0.;points=[]
    previous=None
    for row in sorted(rows,key=lambda r:r['elapsed']):
        if previous is not None:
            dt=row['elapsed']-previous['elapsed']
            if row.get('valid') and previous.get('valid') and 0<dt<=7.5:
                seconds=max(0,min(end,row['time'])-max(start,row['time']-dt))
                value=row['utilization']
                if seconds:
                    covered+=seconds;integral+=value*seconds;congested+=seconds*(value>=.8)
                    excess+=max(0,value-.8)*seconds;points.append((value,seconds))
        previous=row
    cumulative=0.;p95=None
    for value,seconds in sorted(points):
        cumulative+=seconds
        if cumulative>=.95*covered:p95=value;break
    return dict(coverage_seconds=covered,mean_utilization=integral/covered if covered else None,
        congested_link_seconds=congested,excess_utilization_seconds=excess,
        p95_utilization=p95,peak_utilization=max((v for v,_ in points),default=None))


def change_counts(events):
    activated=[e for e in events if e['kind']=='route_activated']
    changed=[e for e in activated if e.get('previous_path') and e['path']!=e['previous_path']]
    return dict(path_changes=len(changed),predictive_changes=sum(e.get('reason')=='predicted_congestion' for e in changed),
        refreshes=sum(e.get('reason')=='refresh_hard_timeout' for e in activated),
        initial_installs=sum(e.get('previous_path') is None for e in activated))


def rows(connection,table):
    for encoded, in connection.execute(f'SELECT record_json FROM {table} ORDER BY id'):
        yield json.loads(encoded)


def inventory(root):
    result={}
    for file in sorted(root.rglob('*')):
        if file.is_file():
            digest=hashlib.sha256()
            with file.open('rb') as handle:
                for block in iter(lambda:handle.read(1024*1024),b''):digest.update(block)
            result[str(file.relative_to(root))]=dict(bytes=file.stat().st_size,sha256=digest.hexdigest())
    return result


def route_key(route):
    return tuple(int(route[k].split(':')[-1],16) for k in ('source_mac','destination_mac'))


def load_run(root,expected_mode,flow_pattern='[0-9][0-9][0-9]-*.json'):
    traffic=json.loads((root/'traffic/traffic.json').read_text());stages=[];flows=[]
    for index,stage in enumerate(traffic['stages']):
        if 'finished_timestamp' not in stage:raise ValueError(f'Unfinished stage {index}')
        stage_flows=[]
        for flow in stage['flows']:
            file=root/'traffic'/Path(flow['iperf_json']).name
            data=json.loads(file.read_text());metric=flow_metrics(data);start=data['start']['test_start']
            if start['duration']!=stage['seconds'] or start['target_bitrate']!=stage['rate_mbps']*1e6:
                raise ValueError(f'iperf settings differ from traffic stage: {file}')
            connection=data['start']['connected'][0]
            if (connection['local_host']!='10.0.0.'+flow['source'][1:] or
                connection['remote_host']!='10.0.0.'+flow['destination'][1:]):
                raise ValueError(f'iperf endpoints differ from traffic stage: {file}')
            if min(metric['sender_seconds'],metric['receiver_seconds'])<stage['seconds']-.1:raise ValueError(f'Shortened transfer: {file}')
            metric.update(stage=index,source=flow['source'],destination=flow['destination'],file=str(file),
                version=data['start']['version'],system=data['start']['system_info'])
            flows.append(metric);stage_flows.append(metric)
        stages.append(dict(index=index,scenario=stage['scenario'],rate_mbps=stage['rate_mbps'],seconds=stage['seconds'],
            reverse=stage['reverse'],planned_elapsed_seconds=stage['planned_elapsed_seconds'],
            start=timestamp(stage['started_timestamp']),end=timestamp(stage['finished_timestamp']),
            actual_elapsed_seconds=stage['actual_elapsed_seconds'],
            wall_seconds=timestamp(stage['finished_timestamp'])-timestamp(stage['started_timestamp']),
            host_pairs=[(f['source'],f['destination']) for f in stage['flows']],metrics=aggregate_flows(stage_flows)))
    expected_files={Path(f['file']).resolve() for f in flows}
    if expected_files!={f.resolve() for f in (root/'traffic').glob(flow_pattern)}:
        raise ValueError('Unexpected/missing stage iperf JSON files')
    start,end=stages[0]['start'],stages[-1]['end']
    with readonly_database(root/'telemetry.sqlite') as c:
        sessions=c.execute('SELECT session_id,started_timestamp,source,config_json FROM sessions').fetchall()
        if len(sessions)!=1:raise ValueError('Expected one session per trial')
        session,session_started,source,encoded=sessions[0];config=json.loads(encoded)
        if source!='live_mininet' or config['routing_mode']!=expected_mode:raise ValueError('Trial provenance/mode mismatch')
        if traffic['session_id']!=session or traffic['routing_mode']!=expected_mode:raise ValueError('Traffic/database session mismatch')
        fields=('scenario','rate_mbps','seconds','reverse','planned_elapsed_seconds')
        actual_plan=[{k:s[k] for k in fields} for s in traffic['stages']]
        if actual_plan!=config['traffic_configuration']['stages']:raise ValueError('Actual stages differ from recorded plan')
        if (sum(s['seconds'] for s in stages)!=traffic['requested_traffic_seconds'] or
            traffic['seed']!=config['traffic_configuration']['seed'] or
            traffic['bandwidth_scale']!=config['bandwidth_scale']):raise ValueError('Trial settings differ from recorded plan')
        for stage in stages:
            pairs=[list(reversed(pair)) if stage['reverse'] else pair for pair in config['traffic_configuration']['flow_pairs']]
            if [list(pair) for pair in stage['host_pairs']]!=pairs:raise ValueError('Traffic endpoints differ from recorded plan')
        events=list(rows(c,'routing_events'));observations=list(rows(c,'observations'));predictions=list(rows(c,'predictions'))
        window_events=[e for e in events if start<=timestamp(e['timestamp'])<=end]
        actual_pairs={tuple(sorted((int(f['source'][1:]),int(f['destination'][1:])))):int(f['source'][1:]) for f in flows}
        tracked_ids={}
        previous={};recommendations=[];trace=[];last_installed={};topologies=set();policies=set();epochs=set();snapshot_count=0
        activations={e['cookie']:dict(e,time_seconds=timestamp(e['timestamp'])-start,
            cookie_hex=hex(e['cookie']),forwarding_evidence=None) for e in window_events if
            e['kind']=='route_activated' and e.get('previous_path') and e['path']!=e['previous_path']}
        baseline_paths={e['route_id']:e['path'] for e in events if e['kind']=='route_activated' and e.get('reason')=='initial_baseline'}
        for activation in activations.values():
            activation['nonbaseline_path']=activation['path']!=baseline_paths[activation['route_id']]
            old_links=[f's{min(u,v)}-s{max(u,v)}' for u,v in zip(activation['previous_path'],activation['previous_path'][1:])]
            latest=[]
            for link in old_links:
                available=[r for r in predictions if r['link_id']==link and
                           timestamp(r['observation_timestamp'])<=timestamp(activation['timestamp'])]
                if available:latest.append(max(available,key=lambda r:timestamp(r['observation_timestamp'])))
            activation['old_path_max_predicted_utilization']=max((r['predicted_utilization'] for r in latest if r.get('status')=='valid'),default=None)
            activation['old_path_max_measured_utilization']=max((r['current_utilization'] for r in latest if r.get('current_utilization') is not None),default=None)
            activation['old_path_forecasts_fresh']=len(latest)==len(old_links) and all(r['status']=='valid' and
                0<=timestamp(activation['timestamp'])-timestamp(r['observation_timestamp'])<=10 for r in latest)
        for snap in rows(c,'routing_snapshots'):
            time=timestamp(snap['timestamp']);inside=start<=time<=end
            if inside:
                snapshot_count+=1;epochs.add(snap['topology_epoch']);policies.add(json.dumps(snap['policy'],sort_keys=True))
                topology=snap['topology']
                physical=dict(connected_switches=topology['connected_switches'],switches=topology['switches'],hosts=topology['hosts'],
                    links=[{k:l[k] for k in ('link_id','source_dpid','destination_dpid','ports','active','capacity_mbps','base_delay_ms')} for l in topology['links']])
                topologies.add(json.dumps(physical,sort_keys=True))
            for route in snap['routes']:
                key=route_key(route);tracked=key in actual_pairs
                if tracked:tracked_ids[route['route_id']]=key
                rec=route.get('recommended_route')
                if rec:
                    old=previous.get(route['route_id']);path=tuple(rec['path']);previous[route['route_id']]=path
                    if inside and old is not None and old!=path:
                        recommendations.append(dict(time_seconds=time-start,route_id=route['route_id'],tracked=tracked,
                            path=list(path),previous_path=list(old),policy=rec['policy']))
                installed=route.get('installed_route');observed=route.get('observed_route')
                if tracked and installed and time<=end:
                    path=tuple(installed['path'])
                    if inside and last_installed.get(key)!=path:trace.append(dict(time_seconds=time-start,pair=list(key),path=list(path)))
                    last_installed[key]=path if inside else None
                if not inside or not installed or not observed:continue
                activation=activations.get(installed['cookie'])
                if not tracked or not activation or observed['cookie']!=installed['cookie']:continue
                counts=[r for r in observed['counters'] if int(r['source_mac'].split(':')[-1],16)==actual_pairs[key]]
                if (not activation['forwarding_evidence'] and len(counts)==len(installed['path']) and
                    {r['dpid'] for r in counts}==set(installed['path']) and all(r['packet_count']>0 for r in counts)):
                    activation['forwarding_evidence']=dict(timestamp=snap['timestamp'],actual_source_host=actual_pairs[key],
                        minimum_hop_packets=min(r['packet_count'] for r in counts),counters=counts)
        table_counts={t:c.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in
                      ('raw_ports','directions','observations','predictions','routing_events','routing_snapshots')}
    bylink=defaultdict(list);window_obs=[]
    for row in observations:
        bylink[row['link_id']].append(dict(time=timestamp(row['observation_timestamp']),elapsed=row['monotonic_elapsed_time'],
            utilization=row.get('utilization'),valid=row.get('valid')))
        if start<=timestamp(row['observation_timestamp'])<=end:window_obs.append(row)
    link_metrics={link:exposure(items,start,end) for link,items in sorted(bylink.items())}
    coverage=sum(r['coverage_seconds'] for r in link_metrics.values())
    utilization=dict(mean_utilization=sum(r['mean_utilization']*r['coverage_seconds'] for r in link_metrics.values())/coverage,
        congestion_link_seconds=sum(r['congested_link_seconds'] for r in link_metrics.values()),
        coverage_link_seconds=coverage,coverage_percent=coverage/(7*(end-start))*100,
        congested_coverage_percent=sum(r['congested_link_seconds'] for r in link_metrics.values())/coverage*100,
        per_link=link_metrics)
    log=(root/'controller.log').read_text();log_issues=[];last_time=None
    for number,line in enumerate(log.splitlines(),1):
        match=re.search(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z',line)
        if match:last_time=timestamp(match.group())
        if any(word in line for word in ('Traceback','ERROR:','WARNING:','AttributeError:')):
            log_issues.append(dict(line=number,text=line,last_timestamped_event=last_time,
                after_traffic=last_time is not None and last_time>end))
    failures=[e for e in window_events if e['kind'] in ('installation_failed','route_quarantined','flow_verification_mismatch','cleanup_retry')]
    changes=list(activations.values());tracked_changes=[e for e in changes if e['route_id'] in tracked_ids]
    dwell=[]
    for key in {e['route_id'] for e in changes}:
        times=[timestamp(e['timestamp']) for e in changes if e['route_id']==key]
        dwell.extend(b-a for a,b in zip(times,times[1:]))
    metrics=aggregate_flows(flows)
    metrics.update(total_received_gb=metrics['received_bytes']/1e9,wall_envelope_seconds=end-start,
        wall_aggregate_goodput_mbps=metrics['received_bytes']*8/(end-start)/1e6,
        nominal_aggregate_goodput_mbps=metrics['received_bytes']*8/(2*traffic['requested_traffic_seconds'])/1e6*2,
        collection_elapsed_seconds=traffic['elapsed_seconds'])
    return dict(root=str(root),session_id=session,session_started=session_started,config=config,traffic=traffic,
        stages=stages,flows=flows,metrics=metrics,start=start,end=end,utilization=utilization,link_series=bylink,
        observation_statuses=dict(Counter(r['status'] for r in window_obs)),valid_window_observations=sum(bool(r.get('valid')) for r in window_obs),
        table_counts=table_counts,model_versions=sorted({r.get('model_version') for r in predictions if r.get('model_version')}),
        prediction_statuses=dict(Counter(r['status'] for r in predictions)),
        traffic_prediction_statuses=dict(Counter(r['status'] for r in predictions if start<=timestamp(r['observation_timestamp'])<=end)),
        first_valid_forecast_relative_seconds=min((timestamp(r['observation_timestamp'])-start for r in predictions if r['status']=='valid'),default=None),
        topology_fingerprints=sorted(topologies),policy_fingerprints=sorted(policies),epochs=sorted(epochs),snapshot_count=snapshot_count,
        recommendations=recommendations,installed_trace=trace,route_changes=changes,tracked_route_changes=tracked_changes,
        route_metrics=dict(**change_counts(window_events),recommended_changes=sum(r['policy']=='predicted' for r in recommendations),
            recommended_changes_tracked=sum(r['policy']=='predicted' and r['tracked'] for r in recommendations),
            candidate_events=sum(e['kind']=='reroute_candidate' for e in window_events),tracked_changes=len(tracked_changes),
            nonbaseline_activations=sum(e['nonbaseline_path'] for e in changes),
            returns_to_baseline=sum(not e['nonbaseline_path'] for e in changes),
            tracked_nonbaseline_activations=sum(e['nonbaseline_path'] for e in tracked_changes),
            tracked_returns_to_baseline=sum(not e['nonbaseline_path'] for e in tracked_changes),
            tracked_changes_positive_hops=sum(bool(e['forwarding_evidence']) for e in tracked_changes),
            verified_path_changes=sum(e.get('verification')=='openflow_flow_stats' for e in changes),
            failures=len(failures),minimum_same_pair_switch_interval_seconds=min(dwell,default=None)),
        event_kind_reason_counts=dict(Counter(f"{e['kind']}:{e.get('reason','')}" for e in window_events)),
        failures=failures,log_issues=log_issues,log_contains_shutdown_hubthread_error="'HubThread' object has no attribute 'kill'" in log,
        collector_exit_status_available=False,latency_mean_ms=None,latency_p95_ms=None)


def compare(baseline,ml):
    keys=('generator','seconds','seed','bandwidth_scale','stages','flow_pairs','measurement_interval_seconds')
    a=baseline['config'];b=ml['config']
    checks=dict(topology_version=a['topology_version']==b['topology_version'],
        traffic_configuration=all(a['traffic_configuration'][k]==b['traffic_configuration'][k] for k in keys),
        settings=all(a[k]==b[k] for k in ('interval_seconds','horizon_seconds','bandwidth_scale','utilization_policy','model_path')),
        model_versions=baseline['model_versions']==ml['model_versions'],
        snapshot_topology=baseline['topology_fingerprints']==ml['topology_fingerprints'] and len(baseline['topology_fingerprints'])==1,
        routing_safety_policy=baseline['policy_fingerprints']==ml['policy_fingerprints'],
        no_runtime_topology_change=all(len(r['epochs'])==1 for r in (baseline,ml)),
        flow_stage_identity=len(baseline['stages'])==len(ml['stages']) and all(all(x[k]==y[k] for k in
            ('scenario','rate_mbps','seconds','reverse','planned_elapsed_seconds','host_pairs')) for x,y in zip(baseline['stages'],ml['stages'])),
        iperf_version_system={(r['version'],r['system']) for r in baseline['flows']}=={(r['version'],r['system']) for r in ml['flows']})
    timing=[dict(stage=x['index'],baseline_wall_seconds=x['wall_seconds'],ml_wall_seconds=y['wall_seconds'],
        wall_seconds_difference=y['wall_seconds']-x['wall_seconds'],
        actual_start_offset_difference_seconds=(y['start']-ml['start'])-(x['start']-baseline['start']))
        for x,y in zip(baseline['stages'],ml['stages'])]
    return dict(checks=checks,recorded_comparability_pass=all(checks.values()),stage_timing=timing,
        maximum_absolute_stage_duration_difference_seconds=max(abs(r['wall_seconds_difference']) for r in timing),
        maximum_absolute_stage_start_offset_difference_seconds=max(abs(r['actual_start_offset_difference_seconds']) for r in timing),
        metrics={key:dict(baseline=baseline['metrics'][key],predictive=ml['metrics'][key],
            **delta(baseline['metrics'][key],ml['metrics'][key])) for key in
            ('sender_mbps','receiver_mbps','wall_aggregate_goodput_mbps','sent_bytes','received_bytes','lost_packets','packets','loss_percent','wall_envelope_seconds','collection_elapsed_seconds')},
        congestion={key:dict(baseline=baseline['utilization'][key],predictive=ml['utilization'][key],
            **delta(baseline['utilization'][key],ml['utilization'][key])) for key in
            ('mean_utilization','congestion_link_seconds','congested_coverage_percent')})


def write_csv(file,records):
    with file.open('w',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(records[0]));writer.writeheader();writer.writerows(records)


def graphs(runs,output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'figure.dpi':130,'savefig.dpi':170,'font.size':10,'svg.hashsalt':'flowrider-phase6b'})
    colors={'baseline':'#187b9d','predictive_ml':'#d66a28'}
    def save(fig,name):
        fig.savefig(output/f'{name}.png',bbox_inches='tight');fig.savefig(output/f'{name}.svg',bbox_inches='tight',metadata={'Date':None});plt.close(fig)
    fig,axes=plt.subplots(2,1,figsize=(10,6),sharex=True,gridspec_kw={'height_ratios':[3,1]})
    for run in runs:
        x=[s['index'] for s in run['stages']];y=[s['metrics']['receiver_mbps']*2 for s in run['stages']]
        axes[0].plot(x,y,'o-',ms=3,label=run['config']['routing_mode'],color=colors[run['config']['routing_mode']])
    axes[0].plot(x,[s['rate_mbps']*2 for s in runs[0]['stages']],'k:',alpha=.7,label='configured aggregate offered rate')
    axes[0].set(ylabel='Aggregate application goodput (Mbps)',title='Matched-stage UDP goodput (two concurrent flows)');axes[0].legend()
    differences=[2*(b['metrics']['receiver_mbps']-a['metrics']['receiver_mbps']) for a,b in zip(runs[0]['stages'],runs[1]['stages'])]
    axes[1].bar(x,differences,color='#777777');axes[1].axhline(0,color='black',lw=.7)
    axes[1].set(xlabel='Matched traffic stage index',ylabel='ML − baseline\n(Mbps)')
    for ax in axes:ax.grid(alpha=.2)
    fig.tight_layout();save(fig,'goodput')
    links=list(runs[0]['link_series']);fig,axes=plt.subplots(2,1,figsize=(11,6),sharex=True)
    vmax=max(1,max(r['utilization']['per_link'][l]['peak_utilization'] or 0 for r in runs for l in links))
    for ax,run in zip(axes,runs):
        for index,link in enumerate(links):
            points=[p for p in run['link_series'][link] if p['valid'] and run['start']<=p['time']<=run['end']]
            im=ax.scatter([p['time']-run['start'] for p in points],[index]*len(points),c=[p['utilization'] for p in points],
                marker='s',s=38,vmin=0,vmax=vmax,cmap='YlOrRd')
        ax.set(yticks=range(len(links)),yticklabels=links,title=run['config']['routing_mode'])
    axes[-1].set_xlabel('Actual seconds since first traffic-stage start (each trial aligned independently)')
    fig.colorbar(im,ax=list(axes),label='Measured utilization (fraction of link capacity)',pad=.02);save(fig,'utilization')
    fig,ax=plt.subplots(figsize=(10,4));x=np.arange(len(links));width=.38
    for index,run in enumerate(runs):
        ax.bar(x+(index-.5)*width,[run['utilization']['per_link'][l]['congested_link_seconds'] for l in links],width,
            label=run['config']['routing_mode'],color=colors[run['config']['routing_mode']])
    ax.set(xticks=x,xticklabels=links,ylabel='Observed link-seconds at utilization ≥80%',title='Congestion exposure (clipped to each traffic window)');ax.legend();fig.tight_layout();save(fig,'congestion-exposure')
    fig,axes=plt.subplots(2,1,figsize=(11,6),sharex=True)
    for ax,pair in zip(axes,([1,4],[3,6])):
        paths=sorted({tuple(p['path']) for run in runs for p in run['installed_trace'] if p['pair']==pair});labels=['→'.join(map(str,p)) for p in paths]
        for run in runs:
            points=[p for p in run['installed_trace'] if p['pair']==pair]
            times=[p['time_seconds'] for p in points]+[run['end']-run['start']]
            values=[paths.index(tuple(p['path'])) for p in points]
            ax.step(times,values+[values[-1]],where='post',label=run['config']['routing_mode'],color=colors[run['config']['routing_mode']])
        ax.set(yticks=range(len(paths)),yticklabels=labels,title=f'h{pair[0]} ↔ h{pair[1]} installed paths (canonical orientation)');ax.legend(loc='upper left')
    axes[-1].set_xlabel('Actual seconds since first stage; brief transaction gaps/outage durations are not measured')
    fig.tight_layout();save(fig,'installed-routes')


def report_markdown(runs,comparison):
    a,b=runs;lines=['# FlowRider Phase 6B: one paired 600-second experiment','',
        '**Conclusion:** real ML rerouting occurred, but these runs do not demonstrate a throughput or loss improvement. RTT/latency was not recorded. This is one pair, not a statistical result.','',
        '## Results','', '| Metric | Baseline | Predictive ML | ML − baseline | Change |','| --- | ---: | ---: | ---: | ---: |']
    def row(label,x,y,decimals=6,suffix='',difference_suffix=None):
        d=delta(x,y);percent=f"{d['percent']:+.4f}%" if d['percent'] is not None else 'N/A (zero baseline)'
        ds=suffix if difference_suffix is None else difference_suffix
        lines.append(f"| {label} | {x:,.{decimals}f}{suffix} | {y:,.{decimals}f}{suffix} | {d['absolute']:+,.{decimals}f}{ds} | {percent} |")
    row('Duration-weighted mean per-flow sender throughput (Mbps)',a['metrics']['sender_mbps'],b['metrics']['sender_mbps'])
    row('Duration-weighted mean per-flow receiver goodput (Mbps)',a['metrics']['receiver_mbps'],b['metrics']['receiver_mbps'])
    row('Aggregate received payload / actual traffic window (Mbps)',a['metrics']['wall_aggregate_goodput_mbps'],b['metrics']['wall_aggregate_goodput_mbps'])
    row('Received payload (bytes)',a['metrics']['received_bytes'],b['metrics']['received_bytes'],0)
    row('Lost UDP datagrams',a['metrics']['lost_packets'],b['metrics']['lost_packets'],0)
    row('UDP loss rate',a['metrics']['loss_percent'],b['metrics']['loss_percent'],6,'%',' pp')
    row('Mean link utilization',a['utilization']['mean_utilization']*100,b['utilization']['mean_utilization']*100,3,'%',' pp')
    row('Congestion exposure ≥80% (link-seconds)',a['utilization']['congestion_link_seconds'],b['utilization']['congestion_link_seconds'],3)
    row('Covered link-time at ≥80%',a['utilization']['congested_coverage_percent'],b['utilization']['congested_coverage_percent'],3,'%',' pp')
    for label,key in [('ML-recommended path changes, all pairs','recommended_changes'),('ML-recommended changes, traffic pairs','recommended_changes_tracked'),
                      ('Qualifying candidate events (not switches)','candidate_events'),('Verified installed path changes, all pairs','verified_path_changes'),
                      ('Installed path changes, traffic pairs','tracked_changes'),('Traffic-pair non-baseline activations','tracked_nonbaseline_activations'),
                      ('Traffic-pair returns to baseline','tracked_returns_to_baseline'),('Traffic-pair changed versions with positive hop counters','tracked_changes_positive_hops'),
                      ('Failed/quarantined transitions','failures')]:row(label,a['route_metrics'][key],b['route_metrics'][key],0)
    lines+=['| Average / P95 RTT or one-way latency | Unavailable | Unavailable | N/A | N/A |','',
        'Sender throughput and receiver goodput use total payload bits divided by the sum of their respective per-flow measured durations; unequal stage durations are not averaged equally. The two concurrent flows imply approximately twice the mean per-flow rate, not a single-flow sum over 1,200 seconds. UDP payload goodput excludes protocol headers. Loss uses total receiver-reported lost datagrams / total expected datagrams.','',
        'Both runs received exactly all their reported sent bytes. ML receiver goodput is slightly lower because receiver reporting intervals are slightly longer, not because measured loss increased. The actual-window aggregate rate also includes stage startup/transition overhead; ML took about 0.667 seconds longer for that window. Payload totals differ by only four 1,448-byte datagrams. Do not interpret these small timing/rounding differences as a causal performance disadvantage or benefit. Stored UDP jitter is not latency, so no RTT mean/P95 was inferred.','',
        f"Sampled ≥80% congestion exposure was {abs(comparison['congestion']['congestion_link_seconds']['percent']):.2f}% {'lower' if comparison['congestion']['congestion_link_seconds']['absolute']<0 else 'higher'} with ML in this pair. This is a link-level observation, not a measured latency/throughput benefit. The max-direction, unweighted mean does not measure total network byte-work or energy efficiency.",'',
        '## Comparability and completion','']
    for key,value in comparison['checks'].items():lines.append(f"- {key}: {'MATCH / PASS' if value else 'MISMATCH / FAIL'}.")
    for run in runs:
        name=run['config']['routing_mode'];m=run['metrics'];u=run['utilization']
        local=datetime.fromtimestamp(run['start'],timezone(timedelta(hours=5,minutes=30))).isoformat()
        lines.append(f"- {name}: 29 stages, 58 complete UDP JSON results; traffic start {local}; valid traffic-window link observations {run['valid_window_observations']}; telemetry coverage {u['coverage_percent']:.3f}% of seven-link window; traffic envelope {m['wall_envelope_seconds']:.6f}s; collection elapsed including tail {m['collection_elapsed_seconds']:.6f}s. Traffic-window prediction statuses: {run['traffic_prediction_statuses']}; first valid forecast {run['first_valid_forecast_relative_seconds']:+.3f}s relative to traffic start.")
    lines += [f"- Maximum matched-stage wall-duration difference: {comparison['maximum_absolute_stage_duration_difference_seconds']:.6f}s. Maximum cumulative aligned stage-start difference: {comparison['maximum_absolute_stage_start_offset_difference_seconds']:.6f}s. See `stages.csv`.",
        '- The recorded plans are identical: seed 42, 600 planned seconds, scale 0.1, topology e279097186c5af01, same recorded model path/version, poll interval and policy. Every stage uses **h4→h1 and h6→h3**, because all saved reverse flags are true; these are the specified host pairs in reverse direction.',
        '- The one topology epoch during each traffic window is stable. Initialization/teardown topology changes outside the window are excluded from congestion/transition counts.',
        '- All expected iperf JSON results contain completed sender/receiver summaries without an iperf error. Client exit codes and collector exit status were not saved, and `traffic.json` is also written in a finally block; complete data is not a formal successful process-exit certificate.',
        '- **Both controller logs contain post-traffic KeyboardInterrupt / HubThread.kill AttributeError shutdown tracebacks.** Their preceding timestamped topology events occur after traffic. No failed/quarantined route transitions or controller critical log lines were found during the measurement window. Clean controller shutdown cannot be claimed; the shutdown error is reported, not repaired in this analysis.',
        '- The matching snapshots/configuration document logical topology, active capacities, delays and policy. Independent applied qdisc/queue logs, run exit status and final ping summaries are not saved here; physical queue equivalence and global cleanup/connectivity completion cannot be independently certified from these directories.',
        '- Trials were sequential (baseline first), with different controller warmup times, telemetry phase alignment, real stage overhead and kernel/CPU scheduling. Frozen-model identity matches by recorded version/path; no per-trial model-byte digest was captured.','',
        '## What routing evidence proves','',
        'Recommendation changes count consecutive path changes in snapshots (first observation excluded), not every poll or recommendation timestamp. Candidate events are qualifying confirmation updates, not successful switches. Installation counts use route_activated with a different previous_path and exact OpenFlow-stat verification, excluding initial installation and same-path hard refresh.', '',
        f"ML had {b['route_metrics']['path_changes']} installed path changes across all pairs ({b['route_metrics']['nonbaseline_activations']} to non-baseline paths, {b['route_metrics']['returns_to_baseline']} returns), including {b['route_metrics']['tracked_changes']} changes on the actual UDP pairs ({b['route_metrics']['tracked_nonbaseline_activations']} non-baseline activations, {b['route_metrics']['tracked_returns_to_baseline']} returns). Their changed versions have {b['route_metrics']['tracked_changes_positive_hops']} saved positive-counter proofs on every hop in the **actual sending direction**. This distinguishes real forwarding from inactive-pair rule installation. Raw independent ovs-ofctl dumps and packet captures were not retained for these benchmark runs; verification here is the controller's persisted exact-flow readback.",
        f"At {sum(e['old_path_forecasts_fresh'] and e['old_path_max_predicted_utilization'] is not None and e['old_path_max_predicted_utilization']>=.8 for e in b['route_changes'])}/{len(b['route_changes'])} ML activations, the previous path had a fresh recorded forecast at or above 80%. Measured utilization on that path was also at least 80% for {sum(e['old_path_max_measured_utilization'] is not None and e['old_path_max_measured_utilization']>=.8 for e in b['route_changes'])}/{len(b['route_changes'])} activations. These predicates are distinct: predictive routing acts on the forecast, not a requirement that current measurements already exceed threshold.",
        f"The minimum interval between two switches for the same pair was {b['route_metrics']['minimum_same_pair_switch_interval_seconds']:.3f}s; no failed transitions/quarantine were recorded. Recommendations varied more frequently than installations, demonstrating confirmation/hysteresis filtering. Multiple returns to previous paths are visible; one trial does not establish general routing stability or bound sub-poll interruption/loss. See `route-changes.csv` and `installed-routes.png`.",'',
        '## Utilization methodology','',
        'Metrics are restricted to each run’s first-stage start through last-stage finish, excluding controller startup and the 20-second collection tail/teardown. Link utilization is max-direction TX / configured capacity, including link-layer traffic overhead; it is not UDP goodput. Valid observations summarize the preceding interval. The script uses adjacent monotonic sample deltas, clips intervals at window edges, rejects invalid samples/gaps over 7.5 seconds and reports covered time. Congestion exposure sums covered seconds at utilization ≥0.8 across links; overlapping links contribute separately (link-seconds, not end-to-end congestion duration). Mean utilization is time-weighted across seven links, without capacity weighting. Sample-window averaging can hide brief bursts.', '',
        '## Graphs and reproducibility','',
        '- `goodput.png` / `.svg`: matched-stage aggregate receiver goodput and ML-minus-baseline differences.',
        '- `utilization.png` / `.svg`: recorded per-link samples aligned to each trial’s actual first-stage start.',
        '- `congestion-exposure.png` / `.svg`: per-link ≥80% exposure over the measured traffic windows.',
        '- `installed-routes.png` / `.svg`: actual installed path records for both traffic pairs; canonical path orientation is shown, while UDP sends in reverse.', '',
        '```bash','~/.pyenv/versions/3.10.16/bin/python docs/phase6b/analyze.py \\',
        '  --baseline data/phase6-benchmark-baseline-001 \\',
        '  --predictive data/phase6-benchmark-predictive-001 --output docs/phase6b',
        '/usr/bin/python3 -m unittest discover -s docs/phase6b -p test_analysis.py','```','',
        'The script opens SQLite in read-only mode, verifies input hashes before/after, and writes only generated documentation artifacts. `results.json` contains definitions, configuration, checks, per-stage metrics, route changes and log classifications. `stages.csv`, `route-changes.csv` and `utilization.csv` expose the numerical graph sources. `input-manifest.json` fingerprints every input file. Existing controller/model/topology code and historical data were not changed. Five focused arithmetic/interpretation unit tests pass, covering duration weighting, packet weighting, missing data, interval clipping/gaps and exclusion of refreshes from route switches.','',
        '## Next experiment','',
        'Repeat matched trials with counterbalanced baseline/ML order and independent repetitions before statistical inference. Add timestamped continuous RTT probes and explicit collector/controller exit/cleanup status, retaining TC/qdisc settings. For a more discriminating load, after warmup try three matched pairs h1→h4 at 60 Mbps, h2→h5 at 80 Mbps and h3→h6 at 80 Mbps: 220 Mbps offered to the shared 200-Mbps backbone while individual host accesses remain below 100 Mbps, with unused alternative directions. Verify actual bottleneck/receiver measurements and spare-path capacity; do not assume the RF will trigger or that rerouting helps. Run exactly the same scenario in both modes. No such new experiment was run here.','']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,default=Path('data/phase6-benchmark-baseline-001'))
    parser.add_argument('--predictive',type=Path,default=Path('data/phase6-benchmark-predictive-001'))
    parser.add_argument('--output',type=Path,default=Path('docs/phase6b'))
    args=parser.parse_args();roots=[args.baseline,args.predictive]
    if any(args.output.resolve().is_relative_to(root.resolve()) for root in roots):parser.error('Output must not be inside raw evidence')
    before={str(root):inventory(root) for root in roots}
    runs=[load_run(args.baseline,'baseline'),load_run(args.predictive,'predictive_ml')];comparison=compare(*runs)
    args.output.mkdir(parents=True,exist_ok=True)
    graphs(runs,args.output)
    write_csv(args.output/'stages.csv',[dict(mode=r['config']['routing_mode'],stage=s['index'],scenario=s['scenario'],
        offered_mbps_per_flow=s['rate_mbps'],planned_seconds=s['seconds'],wall_seconds=s['wall_seconds'],
        actual_elapsed_seconds=s['actual_elapsed_seconds'],sender_mbps_per_flow=s['metrics']['sender_mbps'],
        receiver_mbps_per_flow=s['metrics']['receiver_mbps'],lost_packets=s['metrics']['lost_packets']) for r in runs for s in r['stages']])
    write_csv(args.output/'route-changes.csv',[dict(mode=r['config']['routing_mode'],timestamp=e['timestamp'],
        traffic_elapsed_seconds=e['time_seconds'],route_id=e['route_id'],previous_path=e['previous_path'],path=e['path'],
        reason=e['reason'],cookie_hex=e['cookie_hex'],verification=e['verification'],
        old_path_max_predicted_utilization=e['old_path_max_predicted_utilization'],
        old_path_max_measured_utilization=e['old_path_max_measured_utilization'],
        forecasts_fresh=e['old_path_forecasts_fresh'],nonbaseline_path=e['nonbaseline_path'],
        positive_traffic_direction_hop_counters=bool(e['forwarding_evidence'])) for r in runs for e in r['route_changes']])
    write_csv(args.output/'utilization.csv',[dict(mode=r['config']['routing_mode'],link=link,
        traffic_elapsed_seconds=p['time']-r['start'],utilization=p['utilization'],valid=p['valid'])
        for r in runs for link,points in r['link_series'].items() for p in points if r['start']<=p['time']<=r['end']])
    if before!={str(root):inventory(root) for root in roots}:raise RuntimeError('Input evidence changed during analysis')
    (args.output/'input-manifest.json').write_text(json.dumps(before,indent=2)+'\n')
    (args.output/'results.json').write_text(json.dumps(dict(comparison=comparison,runs=runs,
        latency_note='No RTT or one-way latency samples; UDP jitter is not latency.',
        pairing='One sequential baseline-first pair; no statistical or causal inference.'),indent=2)+'\n')
    (args.output/'report.md').write_text(report_markdown(runs,comparison))
    print(json.dumps(dict(comparability=comparison['recorded_comparability_pass'],metrics=comparison['metrics'],
        congestion=comparison['congestion'],routing=[r['route_metrics'] for r in runs]),indent=2))


if __name__=='__main__':main()
