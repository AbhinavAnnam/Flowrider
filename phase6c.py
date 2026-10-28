"""Shared, standard-library experiment contracts. No routing or model changes."""
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics

from topology_config import TOPOLOGY_VERSION, wired_links

ORDER = ('baseline', 'predictive_ml', 'predictive_ml', 'baseline')
PAIRS = [['h1', 'h4'], ['h3', 'h6']]


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def make_plan(seed=42, bandwidth_mbps=50, queue_packets=100):
    # Chosen before measuring results: 80 Mbps sustained offer to 50 Mbps;
    # even the 90 Mbps burst aggregate is below the 100 Mbps ring capacity.
    stages=make_schedule_fields(seed)
    elapsed=sum(s['seconds'] for s in stages)
    plan=dict(generator='phase6c',seed=seed,seconds=elapsed,bandwidth_scale=.1,
        stages=stages,flow_pairs=PAIRS,measurement_interval_seconds=5,
        probe_interval_seconds=.2,probe_timeout_seconds=1,
        topology_version=TOPOLOGY_VERSION,
        bottleneck=dict(link_id='s2-s5',bandwidth_mbps=bandwidth_mbps,
                        delay_ms=2,queue_packets=queue_packets),
        link_capacities_mbps={'s2-s5':bandwidth_mbps})
    validate_plan(plan)
    return plan


def validate_plan(plan):
    def need(ok,message):
        if not ok:raise ValueError(message)
    if plan.get('topology')=='phase6c':
        queue=plan['bottleneck']['queue_packets']
        need(type(queue) is int and 0<queue<=1000,'Queue must contain 1..1000 packets')
        need(plan==make_four_switch_plan(plan['seed'],queue,plan['profile'],
                                        plan.get('measurement_contract_version',1)),
             'Four-switch plan differs from the preregistered configuration')
        return
    b=plan['bottleneck'];scale=plan['bandwidth_scale']
    need(plan['generator']=='phase6c' and plan['topology_version']==TOPOLOGY_VERSION,'Unsupported topology/plan')
    need(scale==.1 and b['link_id']=='s2-s5','Controlled experiment uses scale .1 and s2-s5')
    low_offer=2*make_schedule_fields(plan['seed'])[0]['rate_mbps']
    need(isinstance(b['bandwidth_mbps'],(int,float)) and math.isfinite(b['bandwidth_mbps']) and
         low_offer<b['bandwidth_mbps']<80,
         'Bottleneck must be finite, above low-load offer and below sustained offered 80 Mbps')
    need(b['delay_ms']==2,'Preserve the configured base propagation delay')
    need(type(b['queue_packets']) is int and 0<b['queue_packets']<=1000,'Queue must contain 1..1000 packets')
    need(plan['link_capacities_mbps']=={'s2-s5':b['bandwidth_mbps']},'Telemetry capacity must equal physical TC rate')
    need(plan['flow_pairs']==PAIRS and plan['measurement_interval_seconds']==5,'Traffic pairs/telemetry interval differ')
    need(plan['probe_interval_seconds']==.2 and plan['probe_timeout_seconds']==1,'Use identical 5 Hz RTT probes with 1s timeout')
    expected=make_schedule_fields(plan['seed'])
    need(plan['stages']==expected,'Traffic stages differ from the preregistered seeded schedule')
    need(plan['seconds']==sum(s['seconds'] for s in expected),'Incomplete traffic duration')


def make_schedule_fields(seed):
    # One schedule generator for plan construction and validation.
    low=random.Random(seed).randint(6,8);elapsed=0;result=[]
    for name,rate,seconds in [('low',low,40),('buildup',18,30),('buildup',30,30),
            ('sustained',40,90),('burst',45,10),('burst',low,10),
            ('burst',45,10),('burst',low,10),('recovery',low,40)]:
        result.append(dict(scenario=name,rate_mbps=rate,seconds=seconds,
            reverse=False,planned_elapsed_seconds=elapsed));elapsed+=seconds
    return result


def create_trial(root,index,plan,model_hash):
    validate_plan(plan)
    if type(index) is not int or not 1<=index<=4:raise ValueError('Trial index must be 1..4 (ABBA)')
    root=Path(root).resolve();mode=ORDER[index-1]
    trial_id=f'{root.name}-{digest(str(root))[:8]}-{index:02d}-{mode}'
    path=root/f'{index:02d}-{mode}'
    path.mkdir(mode=0o700)  # atomic refusal to overwrite any existing evidence
    manifest=dict(trial_id=trial_id,routing_mode=mode,index=index,output=str(path),
        database=str(path/'telemetry.sqlite'),plan_hash=digest(plan),model_sha256=model_hash,
        topology_version=plan['topology_version'],traffic_configuration=plan)
    write_json(path/'trial.json',manifest)
    return manifest


def parse_ping(text,expected_count):
    if type(expected_count) is not int or expected_count<=0:raise ValueError('Expected a positive probe count')
    summary=re.search(r'(\d+) packets transmitted, (\d+) (?:packets )?received,',text)
    if not summary:raise ValueError('Missing completed ping summary')
    sent,received=map(int,summary.groups())
    samples=[dict(timestamp=float(t),sequence=int(seq),rtt_ms=float(rtt)) for t,seq,rtt in
        re.findall(r'^\[([0-9.]+)\].*icmp_seq=(\d+).*time=([0-9.]+) ms',text,re.MULTILINE)]
    if (sent!=expected_count or received>sent or received!=len(samples) or
        len({s['sequence'] for s in samples})!=len(samples) or 'DUP!' in text or
        any(not 1<=s['sequence']<=sent or not math.isfinite(s['rtt_ms']) for s in samples)):
        raise ValueError('Incomplete, duplicate, or untimestamped RTT samples')
    return dict(transmitted=sent,received=received,loss_percent=(sent-received)/sent*100,
                samples=samples,**rtt_metrics(samples))


def rtt_metrics(samples):
    values=sorted(s['rtt_ms'] for s in samples);n=len(values)
    if not n:return dict(sample_count=0,average_ms=None,median_ms=None,p95_ms=None,maximum_ms=None)
    rank=.95*(n-1);lo=int(rank);fraction=rank-lo
    p95=values[lo]*(1-fraction)+values[min(lo+1,n-1)]*fraction
    return dict(sample_count=n,average_ms=statistics.mean(values),median_ms=statistics.median(values),
                p95_ms=p95,maximum_ms=values[-1])


def forwarding_proof(route,source):
    installed=route.get('installed_route') or {};observed=route.get('observed_route') or {}
    counts=[c for c in observed.get('counters',[]) if c['source_mac']==f'00:00:00:00:00:{source:02x}']
    path=installed.get('path',[])
    return bool(path and observed.get('cookie')==installed.get('cookie') and
        len(counts)==len(path) and {c['dpid'] for c in counts}==set(path) and
        all(c['packet_count']>0 for c in counts))


def check_queue(qdisc,packets):
    if not re.search(r'\bnetem\b[^\n]*\blimit '+str(packets)+r'\b',qdisc):
        raise ValueError(f'Applied netem queue does not equal {packets} packets: {qdisc}')


def apply_capacities(topology,capacities,scale):
    """Explicit experiment override, before ports/polling exist; weights untouched."""
    if not isinstance(capacities,dict) or not math.isfinite(scale) or scale<=0:
        raise ValueError('Invalid experiment capacity configuration')
    links={f"s{min(l['src'],l['dst'])}-s{max(l['src'],l['dst'])}":l
           for l in wired_links(getattr(topology,'profile','six'))}
    updates=[]
    for key,value in capacities.items():
        if (key not in links or isinstance(value,bool) or not isinstance(value,(int,float)) or
            not math.isfinite(value) or not 0<value<=1000):
            raise ValueError('Unknown link or invalid physical Mbps capacity')
        link=links[key];updates.append((link['src'],link['dst'],value/scale))
    for u,v,value in updates:topology.graph[u][v]['capacity']=value


def validate_collection(plan,result):
    """Strict completion, not a test that ML must win or change its route."""
    errors=[];stages=result.get('stages',[])
    if len(stages)!=len(plan['stages']):
        errors.append(f"Missing required traffic stages (expected {len(plan['stages'])}, recorded {len(stages)})")
    for i,(expected,stage) in enumerate(zip(plan['stages'],stages)):
        if any(stage.get(k)!=v for k,v in expected.items()):errors.append(f'Stage {i}: traffic plan mismatch')
        if stage.get('status')!='passed':errors.append(f'Stage {i}: traffic generator failure')
        probes=stage.get('probes',[])
        if sorted((p.get('source'),p.get('destination')) for p in probes)!=sorted(map(tuple,plan['flow_pairs'])):
            errors.append(f'Stage {i}: missing probe pairs')
        for probe in probes:
            if probe.get('error'):
                errors.append(f"Stage {i} {probe.get('source')}-{probe.get('destination')}: {probe['error']}")
            # Durations and coverage use one monotonic clock. Keep wall times
            # as raw evidence; NTP/VM clock steps must not consume probe budgets.
            monotonic=(any(k in probe for k in ('started_monotonic','scheduled_last_send_monotonic')) or
                       any(k in stage for k in ('traffic_started_monotonic','traffic_finished_monotonic')))
            start='started_monotonic' if monotonic else 'started'
            last='scheduled_last_send_monotonic' if monotonic else 'scheduled_last_send'
            traffic_start='traffic_started_monotonic' if monotonic else 'traffic_started'
            traffic_end='traffic_finished_monotonic' if monotonic else 'traffic_finished'
            if (not probe.get('complete') or probe.get('exit_code') not in (0,1) or
                probe.get('transmitted')!=round(expected['seconds']/plan['probe_interval_seconds']) or
                probe.get(start,float('inf'))>stage.get(traffic_start,0)+1 or
                probe.get(last,0)<stage.get(traffic_end,float('inf'))-2):
                errors.append(f'Stage {i}: missing probe coverage')
        if set(stage.get('forwarding',{}))!={'-'.join(pair) for pair in plan['flow_pairs']}:errors.append(f'Stage {i}: missing forwarding evidence')
    if not result.get('scoped_cleanup_verified'):errors.append('Missing scoped Mininet cleanup')
    if not result.get('owned_processes_cleaned'):errors.append('Owned process cleanup incomplete')
    if not result.get('tc_verified'):errors.append('Missing applied TC evidence')
    return errors


def make_four_switch_plan(seed=42, queue_packets=100, profile='full', contract_version=2):
    """Preregistered 20 Mbps shared bottleneck; no tuning to measured outcomes."""
    from topology_config import topology_definition
    if profile not in ('full', 'feasibility'):raise ValueError('Unknown benchmark profile')
    if contract_version not in (1,2):raise ValueError('Unknown measurement contract version')
    low=random.Random(seed).randint(1,2)
    durations=([40,30,30,90,10,10,10,10,40] if profile=='full'
               else [25,10,10,45,5,5,5,5,25])
    if contract_version==2:durations=[max(15,seconds) for seconds in durations]
    stages=[];elapsed=0
    for (name,rate),seconds in zip([('low',low),('buildup',6),('buildup',12),
            ('sustained',16),('burst',18),('burst',low),('burst',18),
            ('burst',low),('recovery',low)],durations):
        stages.append(dict(scenario=name,rate_mbps=rate,seconds=seconds,
            reverse=False,planned_elapsed_seconds=elapsed));elapsed+=seconds
    plan=dict(generator='phase6c',topology='phase6c',profile=profile,seed=seed,
        seconds=elapsed,bandwidth_scale=.1,stages=stages,
        flow_pairs=[['h1','h2'],['h3','h4']],measurement_interval_seconds=5,
        probe_interval_seconds=.2,probe_timeout_seconds=1,
        topology_version=topology_definition('phase6c')[2],
        bottleneck=dict(link_id='s2-s4',bandwidth_mbps=20,delay_ms=2,queue_packets=queue_packets),
        link_capacities_mbps={'s2-s4':20})
    if contract_version==2:plan.update(measurement_contract_version=2,telemetry_tail_seconds=20)
    return plan


def expected_tc(plan):
    """Every endpoint, including host namespaces and multiple hosts on S4."""
    from topology_config import topology_definition
    profile=plan.get('topology','six');switches,hosts,_=topology_definition(profile)
    result={};b=plan['bottleneck']
    for link in wired_links(profile):
        key=f"s{min(link['src'],link['dst'])}-s{max(link['src'],link['dst'])}"
        bottleneck=key==b['link_id']
        values=(b['bandwidth_mbps'] if bottleneck else link['capacity']*plan['bandwidth_scale'],
                link['base_delay'], b['queue_packets'] if bottleneck else 1000)
        for end in ('src','dst'):result[f"s{link[end]}-eth{link[end+'_port']}"]=values
    for h in hosts.values():
        result[f"s{h['switch']}-eth{h['port']}"]=(1000*plan['bandwidth_scale'],0,None)
        result[f"{h['name']}-eth0"]=(1000*plan['bandwidth_scale'],0,None)
    return result


def bottleneck_interfaces(plan):
    return [name for name,values in expected_tc(plan).items()
            if values[0]==plan['bottleneck']['bandwidth_mbps'] and values[1]>0]


def forecast_metrics(observations,predictions):
    """Match real +15s forecast targets within the unchanged two-second tolerance.

    Lead time is only counted at a measured rising threshold crossing, with a
    forecast issued while its current observation was below threshold. Routing
    changes affect outcomes, so these are closed-loop descriptive errors.
    """
    from collections import defaultdict
    grouped=defaultdict(list);errors=[];leads=[]
    for row in observations:
        if row.get('valid') and row.get('utilization') is not None:grouped[row['link_id']].append(row)
    for rows in grouped.values():rows.sort(key=lambda r:r['monotonic_elapsed_time'])
    crossings={link:[row['monotonic_elapsed_time'] for prev,row in zip(rows,rows[1:])
        if prev['utilization']<.8<=row['utilization'] and
        row['monotonic_elapsed_time']-prev['monotonic_elapsed_time']<=7]
        for link,rows in grouped.items()}
    for pred in predictions:
        target=pred.get('target_monotonic_elapsed_time');value=pred.get('predicted_utilization')
        rows=grouped.get(pred['link_id'],[])
        if pred.get('status')!='valid' or target is None or value is None or not rows:continue
        actual=min(rows,key=lambda r:abs(r['monotonic_elapsed_time']-target))
        if abs(actual['monotonic_elapsed_time']-target)>2:continue
        errors.append(value-actual['utilization'])
    for link,times in crossings.items():
        rows=grouped[link]
        for crossing in times:
            candidates=[]
            for pred in predictions:
                target=pred.get('target_monotonic_elapsed_time');value=pred.get('predicted_utilization')
                if pred['link_id']!=link or pred.get('status')!='valid' or target is None or value is None or value<.8:continue
                issue=target-15
                current=min(rows,key=lambda r:abs(r['monotonic_elapsed_time']-issue))
                if abs(target-crossing)<=2 and issue<crossing and abs(current['monotonic_elapsed_time']-issue)<=2 and current['utilization']<.8:
                    candidates.append(crossing-issue)
            if candidates:leads.append(max(candidates))
    return dict(status='available' if errors else 'unavailable_no_matched_targets',matched_count=len(errors),
        mae=statistics.mean(map(abs,errors)) if errors else None,
        rmse=math.sqrt(statistics.mean(e*e for e in errors)) if errors else None,
        measured_rising_crossings=sum(map(len,crossings.values())),
        anticipated_crossings=len(leads),lead_times_seconds=leads,
        mean_lead_seconds=statistics.mean(leads) if leads else None)
