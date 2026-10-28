"""Read-only decision/model audit of a completed Phase 6C pair. No live traffic."""
import argparse
from collections import Counter
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import statistics
import sys

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
import networkx as nx
from prediction import FEATURES,evaluate,feature_for,load_model
from research_phase3 import audit_predictions
from phase6c import write_json
spec=importlib.util.spec_from_file_location('phase6c_analysis',ROOT/'docs/phase6c/analyze.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
PAIRS=((1,2),(3,4))


def stats(values):
    return dict(n=len(values),minimum=min(values,default=None),mean=statistics.mean(values) if values else None,
                maximum=max(values,default=None))


def audit_trial(trial,bundle):
    manifest=json.loads((trial/'trial.json').read_text())
    collection=json.loads((trial/'collection.json').read_text());stages=collection['stages']
    start=a.b.timestamp(stages[0]['started_timestamp']);end=a.b.timestamp(stages[-1]['finished_timestamp'])
    validation=a.validate_trial(trial)
    assert not validation['errors'],validation['errors']
    with a.b.readonly_database(trial/'telemetry.sqlite') as connection:
        observations=list(a.b.rows(connection,'observations'));predictions=list(a.b.rows(connection,'predictions'))
        snapshots=list(a.b.rows(connection,'routing_snapshots'));events=list(a.b.rows(connection,'routing_events'))
    window_predictions=[p for p in predictions if start<=a.b.timestamp(p['observation_timestamp'])<=end]
    forecast_audit=audit_predictions(observations,window_predictions,bundle['metadata'])
    matched=forecast_audit['matured_pairs'];links={};sensitivity=[];forest=bundle['model']
    for link in sorted({r['link_id'] for r in observations}):
        history=[r for r in observations if r['link_id']==link]
        rows=[r for r in history if r['valid'] and start<=a.b.timestamp(r['observation_timestamp'])<=end]
        forecasts={p['observation_timestamp']:p for p in window_predictions if p['link_id']==link and p['status']=='valid'}
        pairs=[p for p in matched if p['link_id']==link]
        congested=[r for r in rows if r['utilization']>=.8 and r['observation_timestamp'] in forecasts]
        links[link]=dict(measured=stats([r['utilization'] for r in rows]),
            predicted=stats([p['predicted_utilization'] for p in forecasts.values()]),
            predicted_congestion_count=sum(p['predicted_utilization']>=.8 for p in forecasts.values()),
            measured_congested_count=len(congested),
            forecast_during_measured_congestion=stats([forecasts[r['observation_timestamp']]['predicted_utilization'] for r in congested]),
            matured_accuracy=evaluate([p['actual_future_utilization'] for p in pairs],[p['predicted_utilization'] for p in pairs]),
            actual_horizon_seconds=stats([p['actual_horizon_seconds'] for p in pairs]))
        if link!='s2-s4':continue
        assert all(r['capacity_mbps']==20 and math.isclose(
            r['utilization'],r['tx_mbps']/20,abs_tol=1e-12) for r in rows)
        for index,row in enumerate(history):
            pred=forecasts.get(row['observation_timestamp']);features=feature_for(history[max(0,index-4):index+1])
            if pred is None or features is None:continue
            assert math.isclose(float(forest.predict([features])[0]),pred['raw_predicted_utilization'],abs_tol=1e-12)
            current=[r for r in pairs if r['observation_timestamp']==row['observation_timestamp']]
            for capacity in (20,100,200):
                proper=features.copy();proper[7]*=capacity/20;proper[8]*=capacity/20;proper[9]=capacity
                cap_only=features.copy();cap_only[9]=capacity
                sensitivity.append(dict(timestamp=row['observation_timestamp'],current_utilization=row['utilization'],
                    future_utilization=current[0]['actual_future_utilization'] if current else None,
                    capacity_mbps=capacity,recorded_prediction=pred['predicted_utilization'],
                    capacity_only_prediction=float(forest.predict([cap_only])[0]),
                    scaled_mbps_prediction=float(forest.predict([proper])[0])))
    installed=[]
    for stage in stages:
        for source,destination in PAIRS:
            routes=[r for snap in snapshots if a.b.timestamp(stage['started_timestamp'])<=a.b.timestamp(snap['timestamp'])<=a.b.timestamp(stage['finished_timestamp'])
                    for r in snap['routes'] if a.b.route_key(r)==(source,destination)]
            paths=sorted({tuple(r['installed_route']['path']) for r in routes if r.get('installed_route')})
            proof=stage['forwarding'][f'h{source}-h{destination}'];reverse=stage['bidirectional_forwarding'][f'h{source}-h{destination}']
            assert a.forwarding_proof(proof['route'],source) and a.forwarding_proof(reverse['route'],destination)
            installed.append(dict(mode=manifest['routing_mode'],stage=stage['index'],scenario=stage['scenario'],
                source=source,destination=destination,installed_paths=[list(p) for p in paths],
                proof_cookie=proof['route']['installed_route']['cookie'],
                cutover_snapshots_without_installed=sum(not r.get('installed_route') for r in routes),bidirectional_positive_proof=True))
    decisions=[];seen=set()
    for snapshot in snapshots:
        if not start<=a.b.timestamp(snapshot['timestamp'])<=end:continue
        for route in snapshot['routes']:
            pair=a.b.route_key(route);rec=route.get('recommended_route')
            if pair not in PAIRS or not rec or (route['route_id'],rec['timestamp']) in seen:continue
            seen.add((route['route_id'],rec['timestamp']));when=a.b.timestamp(rec['timestamp'])
            if not start<=when<=end:continue
            active=route.get('installed_route');path=active['path'] if active else None
            record=dict(mode=manifest['routing_mode'],source=pair[0],destination=pair[1],timestamp=rec['timestamp'],
                seconds=when-start,stage=next((s['index'] for s in stages if a.b.timestamp(s['started_timestamp'])<=when<=a.b.timestamp(s['finished_timestamp'])),None),
                policy=rec['policy'],recommended_path=rec['path'],installed_path=path,
                transaction=route['transaction_phase'],reason='baseline_static' if manifest['routing_mode']=='baseline' else 'forecast_unavailable',
                installed_cost=None,alternative_cost=None,relative_gain=None,cooldown_elapsed=None,
                fresh_forecasts=False,forecast_gate=False)
            if rec['policy']=='predicted':
                graph=nx.Graph();by_id={}
                for link in snapshot['topology']['links']:
                    p=link['prediction'];o=link['measurement'];by_id[link['link_id']]=link
                    age=when-a.b.timestamp(p['observation_timestamp'])
                    assert p['status']=='valid' and o['valid'] and link['active'] and 0<=age<=10
                    assert p['observation_timestamp']==o['observation_timestamp'] and p['prediction_horizon_seconds']==15
                    weight=link['base_delay_ms']*(1+25*p['predicted_utilization']**4)
                    assert math.isclose(weight,link['routing_weight'],rel_tol=1e-12)
                    graph.add_edge(link['source_dpid'],link['destination_dpid'],weight=weight)
                    record[link['link_id']+'_predicted']=p['predicted_utilization']
                    record[link['link_id']+'_measured']=o['utilization']
                record['fresh_forecasts']=True
                cost=lambda nodes:sum(graph[u][v]['weight'] for u,v in zip(nodes,nodes[1:]))
                assert math.isclose(cost(rec['path']),rec['weighted_cost'],rel_tol=1e-12)
                best=nx.shortest_path(graph,1 if pair==(1,2) else 2,4,weight='weight')
                assert best==rec['path']
                alternative=[1,3,4] if pair==(1,2) else [2,1,3,4]
                assert all(graph.has_edge(u,v) for u,v in zip(alternative,alternative[1:]))
                record['alternative_cost']=cost(alternative)
                if path:
                    record['installed_cost']=cost(path);record['relative_gain']=(cost(path)-cost(rec['path']))/cost(path)
                    record['forecast_gate']=any(by_id[f's{min(u,v)}-s{max(u,v)}']['prediction']['predicted_utilization']>=.8 for u,v in zip(path,path[1:]))
                    activations=[e for e in events if e['kind']=='route_activated' and e['route_id']==route['route_id'] and a.b.timestamp(e['timestamp'])<=when
                        and not(e.get('reason')=='refresh_hard_timeout' and e.get('previous_path')==e['path'])]
                    record['cooldown_elapsed']=when-max(a.b.timestamp(e['timestamp']) for e in activations)
                    policy=snapshot['policy']
                    record['reason']=('transaction_in_progress' if route['transaction_phase'] else 'recommended_installed_path' if rec['path']==path else
                        'cooldown' if record['cooldown_elapsed']<policy['cooldown'] else
                        'gain_below_10_and_forecast_below_80' if record['relative_gain']<policy['min_improvement'] and not record['forecast_gate'] else
                        'gain_below_10' if record['relative_gain']<policy['min_improvement'] else
                        'forecast_below_80' if not record['forecast_gate'] else 'candidate_eligible')
                else:record['reason']='no_installed_version'
            decisions.append(record)
    summary=validation['summary']
    summary['per_link_forecasts']=links
    summary['decision_counts']={f'h{src}-h{dst}':dict(frames=len(rows:=[r for r in decisions if (r['source'],r['destination'])==(src,dst)]),
        alternative_frames=sum(bool(r['installed_path']) and r['recommended_path']!=r['installed_path'] for r in rows),
        reasons=dict(Counter(r['reason'] for r in rows))) for src,dst in PAIRS}
    summary['events']=dict(Counter(e['kind']+':'+str(e.get('reason')) for e in events if start<=a.b.timestamp(e['timestamp'])<=end))
    summary['forecast_audit']=forecast_audit['evaluation']
    return summary,decisions,installed,sensitivity


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',required=True,type=Path);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();output=args.output.resolve();evidence=args.experiment.resolve()
    assert not output.exists(),'Refuse to overwrite analysis artifacts'
    assert evidence not in output.parents and evidence!=output
    before=a.b.inventory(evidence)
    model=ROOT/'data/research-20261015T180515Z/evaluation/model.pkl';model_hash=hashlib.sha256(model.read_bytes()).hexdigest()
    bundle=load_model(model);trials=[];decisions=[];installed=[];sensitivity=[]
    for trial in sorted((evidence/'trials').glob('0*')):
        assert json.loads((trial/'trial.json').read_text())['model_sha256']==model_hash
        summary,d,i,s=audit_trial(trial,bundle);trials.append(summary);decisions+=d;installed+=i
        sensitivity.extend(dict(mode=summary['routing_mode'],**r) for r in s)
    assert len(trials)==2
    thresholds=sorted({float(t.tree_.threshold[i]) for t in bundle['model'].estimators_ for i,feature in enumerate(t.tree_.feature) if feature==9})
    assert before==a.b.inventory(evidence),'Evidence changed during analysis'
    assert model_hash==hashlib.sha256(model.read_bytes()).hexdigest()
    output.mkdir(parents=True)
    # Store compact diagnosis sources rather than duplicating all recorded probes.
    keep=('trial_id','routing_mode','rtt','icmp_loss_percent','loss_percent','congestion_link_seconds','queue_drops','alternative_path_utilization',
          'forecast_accuracy','routing','comparability','per_link_forecasts','decision_counts','events','forecast_audit')
    write_json(output/'results.json',dict(model_sha256=model_hash,capacity_split_thresholds=thresholds,
        feature_importances=dict(zip(FEATURES,bundle['model'].feature_importances_.tolist())),
        trials=[{k:t[k] for k in keep} for t in trials]))
    write_json(output/'input-manifest.json',before)
    for name,records in [('decisions',decisions),('installed-paths',installed),('synthetic-capacity-sensitivity',sensitivity)]:
        with (output/f'{name}.csv').open('w',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=sorted(set().union(*(r.keys() for r in records))));writer.writeheader()
            writer.writerows({k:json.dumps(v) if isinstance(v,(list,dict)) else v for k,v in r.items()} for r in records)
    print(json.dumps(dict(output=str(output),trials=[dict(mode=t['routing_mode'],decisions=t['decision_counts']) for t in trials]),indent=2))


if __name__=='__main__':main()
