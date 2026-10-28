"""Run verified four-switch experiments or a console-only six-switch comparison."""
import argparse
from collections import Counter
import hashlib
import json
import os
import pwd
import shutil
from datetime import datetime, timezone
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from phase6c import ORDER, create_trial, digest, make_plan, make_four_switch_plan, make_six_switch_plan, validate_plan, write_json


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stop_owned(process,seconds):
    if process is None:return None
    if process.poll() is None:process.terminate()
    try:return process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        process.kill();process.wait(timeout=5)
        return process.returncode


def controller_python(explicit=None):
    if explicit:return str(Path(explicit).expanduser().resolve())
    home=Path(pwd.getpwnam(os.environ.get('SUDO_USER') or pwd.getpwuid(os.getuid()).pw_name).pw_dir)
    candidate=home/'.pyenv/versions/3.10.16/bin/python'
    return str(candidate) if candidate.is_file() else sys.executable


def resource_check():
    memory={line.split(':')[0]:int(line.split()[1])*1024
            for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')}
    result=dict(free_disk_bytes=shutil.disk_usage(ROOT).free,
                available_memory_bytes=memory['MemAvailable'],load_average=os.getloadavg())
    if min(result['free_disk_bytes'],result['available_memory_bytes'])<256*1024**2:
        raise RuntimeError('Benchmark requires at least 256 MiB free disk and available memory')
    return result


def routing_reasons(snapshots,events,pairs,start,end):
    """Reconstruct no-reroute gates; no predictions or forwarding are altered."""
    from datetime import datetime
    stamp=lambda value:datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    names={tuple(sorted((int(src[1:]),int(dst[1:])))):f'{src}-{dst}' for src,dst in pairs}
    reasons={name:Counter() for name in names.values()};seen=set();route_names={}
    for snapshot in snapshots:
        links=snapshot['topology']['links'];policy=snapshot['policy']
        weights={frozenset((l['source_dpid'],l['destination_dpid'])):l.get('routing_weight') for l in links}
        forecasts={frozenset((l['source_dpid'],l['destination_dpid'])):(l.get('prediction') or {}).get('predicted_utilization',0) for l in links}
        for route in snapshot['routes']:
            key=tuple(int(route[k].split(':')[-1],16) for k in ('source_mac','destination_mac'))
            if key not in names:continue
            name=names[key];route_names[route['route_id']]=name;rec=route.get('recommended_route')
            if not rec or not start<=stamp(rec['timestamp'])<=end:continue
            identity=(route['route_id'],rec['timestamp'])
            if identity in seen:continue
            seen.add(identity);installed=route.get('installed_route')
            if rec['policy']!='predicted':reason='prediction freshness/history'
            elif not installed or route.get('transaction_phase'):reason='forwarding safety/busy'
            elif installed['path']==rec['path']:reason='route selection'
            else:
                activations=[e for e in events if e['kind']=='route_activated' and e['route_id']==route['route_id'] and
                    stamp(e['timestamp'])<=stamp(rec['timestamp']) and not
                    (e.get('reason')=='refresh_hard_timeout' and e.get('previous_path')==e['path'])]
                last=max((stamp(e['timestamp']) for e in activations),default=float('-inf'))
                edges=lambda path:[frozenset((u,v)) for u,v in zip(path,path[1:])]
                old=edges(installed['path']);new=edges(rec['path'])
                if any(weights.get(edge) is None for edge in old+new):reason='prediction freshness/history'
                elif stamp(rec['timestamp'])-last<policy.get('cooldown',30):reason='cooldown'
                else:
                    old_cost=sum(weights[edge] for edge in old)
                    gain=(old_cost-sum(weights[edge] for edge in new))/old_cost
                    if gain<policy.get('min_improvement',.1):reason='route selection (<10% gain)'
                    elif not any(forecasts[edge]>=.8 for edge in old):reason='prediction gate'
                    else:reason='confirmation'
            reasons[name][reason]+=1
    for event in events:
        if event['kind'] in ('installation_failed','flow_verification_mismatch','route_quarantined') and start<=stamp(event['timestamp'])<=end:
            name=route_names.get(event['route_id'])
            if name:reasons[name]['installation failure']+=1
    return reasons


def print_console(experiment):
    """Print only measured results. No report/figure output is created."""
    sys.path.insert(0,str(ROOT/'docs/phase6c'))
    from analyze import aggregate,b,rtt_metrics
    config=json.loads((experiment/'experiment.json').read_text());summaries=[];identities=[]
    print('\nMeasured six-switch comparison (one pair is descriptive only):',flush=True)
    for index,mode in enumerate(ORDER[:config['trial_count']],1):
        trial=experiment/f'{index:02d}-{mode}'
        if not (trial/'validation.json').is_file():
            print(f'{mode}: UNAVAILABLE (trial did not finish)');continue
        result=json.loads((trial/'validation.json').read_text());status=json.loads((trial/'trial-status.json').read_text())
        manifest=json.loads((trial/'trial.json').read_text())
        if (manifest['plan_hash']!=config['plan_hash'] or manifest['model_sha256']!=config['model_sha256'] or
                manifest['source_sha256']!=config['source_sha256']):raise RuntimeError('Trial plan/model/source differs')
        if result['errors'] or status['status']!='passed':
            print(f'{mode}: INVALID: '+('; '.join(result['errors']) or status.get('validation_error','trial failed')))
        else:summaries.append(result['summary']);identities.append(result['summary']['comparability'])
        if (trial/'telemetry.sqlite').exists() and (trial/'collection.json').is_file():
            traffic=json.loads((trial/'collection.json').read_text());stages=traffic['stages']
            if not stages:continue
            start=b.timestamp(stages[0]['started_timestamp']);end=b.timestamp(stages[-1]['finished_timestamp'])
            with b.readonly_database(trial/'telemetry.sqlite') as connection:
                snapshots=list(b.rows(connection,'routing_snapshots'));events=list(b.rows(connection,'routing_events'))
                predictions=[p for p in b.rows(connection,'predictions') if p['link_id']=='s2-s5' and p.get('status')=='valid' and start<=b.timestamp(p['observation_timestamp'])<=end]
            values=[p['predicted_utilization'] for p in predictions]
            print(f'{mode} S2-S5 RF: n={len(values)}, range='+
                (f'{min(values):.3f}..{max(values):.3f}, >=80%={sum(v>=.8 for v in values)}' if values else 'unavailable'))
            if mode=='predictive_ml' and (not result.get('summary') or not any(
                    e.get('reason')=='predicted_congestion' and e.get('forwarding_evidence') for e in result['summary']['route_changes'])):
                for pair,counts in routing_reasons(snapshots,events,config['plan']['flow_pairs'],start,end).items():
                    print(f'  {pair} no verified ML reroute: '+', '.join(f'{key}={value}' for key,value in counts.items()))
    if len(summaries)!=config['trial_count']:raise RuntimeError('Incomplete/invalid comparison; retain raw diagnostics')
    if len({digest(value) for value in identities})!=1:raise RuntimeError('Topology/policy/model/platform differ between trials')
    groups=aggregate(summaries)
    print(f"{'Metric (whole traffic window)':38s} {'Baseline':>12s} {'Predictive ML':>14s}")
    for key,label in [('average_ms','Average RTT (ms)'),('p95_ms','P95 RTT (ms)'),('icmp_loss_percent','ICMP packet loss (%)'),
            ('wall_aggregate_goodput_mbps','Aggregate UDP goodput (Mbps)'),('loss_percent','UDP packet loss (%)'),('congestion_link_seconds','Congestion exposure (link-seconds)')]:
        values=[groups[m]['metrics'][key]['mean'] for m in ('baseline','predictive_ml')]
        print(f'{label:38s} '+ ' '.join(f'{v:12.3f}' if v is not None else f'{"unavailable":>12s}' for v in values))
    for trial in summaries:
        sustained=[s for s in trial['stages'] if s['scenario']=='sustained']
        probes=[p for s in sustained for p in s['pairs']];samples=[v for p in probes for v in p['samples']]
        rtt=rtt_metrics(samples);udp=[s for s in trial['traffic_stages'] if s['scenario']=='sustained']
        loss=100*sum(p['transmitted']-p['received'] for p in probes)/sum(p['transmitted'] for p in probes)
        goodput=sum(s['metrics']['received_bytes'] for s in udp)*8/sum(s['wall_seconds'] for s in udp)/1e6
        print(f"{trial['routing_mode']} sustained: RTT avg/P95={rtt['average_ms']}/{rtt['p95_ms']} ms, ICMP loss={loss:.3f}%, "
              f"UDP={goodput:.3f} Mbps, UDP loss={b.aggregate_flows([s['metrics'] for s in udp])['loss_percent']:.3f}%, "
              f"trial verified traffic path changes={trial['routing']['tracked_changes_positive_hops']}, "
              f"ML-triggered={sum(e.get('reason')=='predicted_congestion' and bool(e.get('forwarding_evidence')) for e in trial['route_changes'])}")


def run_benchmark(args):
    """One command, bounded child processes, immutable evidence, no global cleanup."""
    quick=args.profile=='six-switch'
    if not quick and args.topology!='phase6c':raise ValueError('Automatic benchmark uses the independent phase6c topology')
    if os.geteuid()!=0:
        try:subprocess.run(['sudo','-n','true'],check=True)
        except subprocess.CalledProcessError as error:
            command='--profile six-switch' if quick else '--trials 4'
            raise RuntimeError('Authenticated sudo unavailable. Run: sudo /usr/bin/python3 scripts/run_phase6c.py '+command) from error
    python=controller_python(args.controller_python)
    subprocess.run([python,'-c','import os_ken, networkx, sklearn'+('' if quick else ', matplotlib')],check=True)
    subprocess.run(['/usr/bin/python3','-c','import mininet'],check=True)
    for binary in ('ovs-vsctl','ovs-ofctl','tc','iperf3','ping','ip'):
        if shutil.which(binary) is None:raise RuntimeError(f'Missing required executable: {binary}')
    resources=resource_check()
    experiment=(args.experiment or ROOT/'data'/datetime.now(timezone.utc).strftime('phase6c-%Y%m%dT%H%M%S%fZ')).resolve()
    if experiment.exists():raise FileExistsError(f'Refusing to overwrite {experiment}')
    experiment.mkdir(mode=0o700,parents=True)
    write_json(experiment/'resources.json',resources)
    if quick:
        subprocess.run([python,'-c','from prediction import load_model; import sys; load_model(sys.argv[1])',str(args.model)],check=True)
    else:
        subprocess.run([python,str(ROOT/'scripts/check_phase6c_model.py'),'--model',str(args.model),
                        '--output',str(experiment/'model-compatibility.json')],check=True)
    common=['--model',str(args.model),'--seed',str(args.seed),'--queue-packets',str(args.queue_packets),
            '--controller-port',str(args.controller_port),'--controller-python',python,'--topology',args.topology]
    def series(name,profile,count):
        root=experiment/name
        command=[sys.executable,str(Path(__file__).resolve()),'--experiment',str(root),*common,'--profile',profile]
        subprocess.run([*command,'--init'],check=True)
        record=json.loads((root/'experiment.json').read_text());record['trial_count']=count
        write_json(root/'experiment.json',record)
        failure=None
        try:
            for index in range(1,count+1):
                print(f'Phase 6C {name} trial {index}/{count}: {ORDER[index-1]}',flush=True)
                subprocess.run([*command,'--trial',str(index)],check=True)
        except BaseException as error:
            failure=error
        finally:
            # Failed trials retain diagnostics; never substitute measurements.
            report=experiment/f'{name}-analysis'
            if quick:
                print_console(root)
                analysis=None
            else:
                analysis=subprocess.run([python,str(ROOT/'docs/phase6c/analyze.py'),
                    '--experiment',str(root),'--output',str(report)])
        if failure:raise failure
        if analysis is not None and analysis.returncode:raise RuntimeError(f'Invalid comparison: {report}/report.md')
    if args.profile=='full':
        series('feasibility','feasibility',2)
        write_json(experiment/'resources-before-full.json',resource_check())
    series('trials',args.profile,args.trials)
    print(f'Completed benchmark: {experiment}'+('' if quick else '/trials-analysis/report.md'),flush=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',type=Path)
    action=parser.add_mutually_exclusive_group()
    action.add_argument('--init',action='store_true')
    action.add_argument('--trial',type=int,choices=range(1,5))
    action.add_argument('--trials',type=int,choices=(2,4))
    parser.add_argument('--profile',choices=['full','feasibility','six-switch'],default='full')
    parser.add_argument('--topology',choices=['six','phase6c'],default='phase6c')
    parser.add_argument('--controller-python')
    parser.add_argument('--model',type=Path,default=ROOT/'data/research-20261015T180515Z/evaluation/model.pkl')
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--bottleneck-mbps',type=float,default=None,help='Legacy six-switch experiments only')
    parser.add_argument('--queue-packets',type=int,default=None)
    parser.add_argument('--controller-port',type=int,default=16653)
    args=parser.parse_args(argv);os.chdir(ROOT)
    quick=args.profile=='six-switch'
    if quick:args.topology='six'
    if args.queue_packets is None:args.queue_packets=1000 if quick else 100
    if quick and (args.queue_packets!=1000 or args.bottleneck_mbps is not None):
        parser.error('Six-switch preset preserves the existing capacities and 1000-packet queues')
    args.model=args.model.resolve()
    if not args.init and args.trial is None:
        args.trials=args.trials or (2 if args.profile in ('feasibility','six-switch') else 4)
        if args.profile=='full' and args.trials!=4:parser.error('Complete trials require four counterbalanced ABBA runs')
        if args.bottleneck_mbps is not None:parser.error('Four-switch benchmark has a fixed 20 Mbps bottleneck')
        run_benchmark(args)
        return
    if args.experiment is None:parser.error('--experiment is required with --init or --trial')
    args.experiment=args.experiment.resolve()
    model_hash=file_hash(args.model)
    source_hashes={f:file_hash(ROOT/f) for f in ('controller.py','predictive_routing.py','prediction.py',
        'telemetry.py','telemetry_runtime.py','topology_config.py','network_topology.py','mininet_topology.py',
        'collect_phase6c.py','phase6c.py','scripts/run_phase6c.py','docs/phase6c/analyze.py',
        'docs/phase6b/analyze.py','telemetry_store.py','topology_state.py',
        'tests/integration_phase5.py','tests/integration_phase2.py','experimental_topology.py',
        'scripts/check_phase6c_model.py')}
    if args.init:
        if args.topology=='phase6c' and args.bottleneck_mbps is not None:
            parser.error('Four-switch benchmark has a fixed 20 Mbps bottleneck')
        plan=(make_six_switch_plan(args.seed) if quick else make_four_switch_plan(args.seed,args.queue_packets,args.profile) if args.topology=='phase6c'
              else make_plan(args.seed,args.bottleneck_mbps or 50,args.queue_packets))
        validate_plan(plan)
        args.experiment.mkdir(mode=0o700)  # no overwrite, including older phases
        write_json(args.experiment/'experiment.json',dict(order=ORDER,plan=plan,plan_hash=digest(plan),
            model=str(args.model),model_sha256=model_hash,source_sha256=source_hashes))
        print(f'Created plan only: {args.experiment}/experiment.json; {plan["seconds"]} traffic seconds per trial')
        return
    experiment=json.loads((args.experiment/'experiment.json').read_text());plan=experiment['plan'];validate_plan(plan)
    if (experiment['model_sha256']!=model_hash or experiment['source_sha256']!=source_hashes or
        experiment['plan_hash']!=digest(plan) or experiment['order']!=list(ORDER)):
        parser.error('Model, source or plan changed: start a new experiment; retain existing evidence')
    for index in range(1,args.trial):
        prior=args.experiment/f'{index:02d}-{ORDER[index-1]}'/'trial-status.json'
        if not prior.is_file() or json.loads(prior.read_text()).get('status')!='passed':
            parser.error('Run ABBA in order; prior trial must finish with verified cleanup and complete data')
    with socket.socket() as port_check:port_check.bind(('127.0.0.1',args.controller_port))
    if os.geteuid()!=0:subprocess.run(['sudo','-n','true'],check=True)
    manifest=create_trial(args.experiment,args.trial,plan,model_hash)
    output=Path(manifest['output']);write_json(output/'plan.json',plan)
    manifest['source_sha256']=source_hashes;write_json(output/'trial.json',manifest)
    controller=collector=None
    status=dict(status='failed',collector_exit_code=None,controller_exit_code=None,
                controller_stop_requested=False,interrupted=False)
    def interrupted(signum,frame):
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        raise KeyboardInterrupt(f'Signal {signum}')
    signal.signal(signal.SIGTERM,interrupted)
    try:
        with (output/'controller.log').open('w') as controller_log,(output/'mininet.log').open('w') as mininet_log:
            try:
                controller=subprocess.Popen([controller_python(args.controller_python),'-u','controller.py','--ofp-listen-host','127.0.0.1',
                    '--topology',plan.get('topology','six'),
                    '--ofp-tcp-listen-port',str(args.controller_port),'--routing-mode',manifest['routing_mode'],
                    '--telemetry-enabled','--telemetry-bandwidth-scale',str(plan['bandwidth_scale']),
                    '--telemetry-db',manifest['database'],'--telemetry-model',str(args.model),
                    '--trial-id',manifest['trial_id'],'--traffic-config',str(output/'plan.json'),
                    '--experiment-link-capacities',json.dumps(plan['link_capacities_mbps'])],
                    stdout=controller_log,stderr=subprocess.STDOUT)
                until=time.monotonic()+12
                while True:
                    if controller.poll() is not None:raise RuntimeError('Controller exited during startup')
                    try:
                        with socket.create_connection(('127.0.0.1',args.controller_port),timeout=.3):break
                    except OSError:
                        if time.monotonic()>=until:raise RuntimeError('Controller startup exceeded 12 seconds')
                        time.sleep(.1)
                collector=subprocess.Popen(([] if os.geteuid()==0 else ['sudo','-n'])+['/usr/bin/python3','-u','collect_phase6c.py',
                    '--output',str(output),'--controller-port',str(args.controller_port)],
                    stdout=mininet_log,stderr=subprocess.STDOUT)
                # Planned send time + bounded setup/result/cleanup per stage + lab startup.
                deadline=time.monotonic()+plan['seconds']+20*len(plan['stages'])+60
                while collector.poll() is None:
                    if controller.poll() is not None:raise RuntimeError('Controller exited during measurement')
                    if time.monotonic()>deadline:raise RuntimeError('Collector exceeded its plan-derived deadline')
                    time.sleep(.5)
                status['collector_exit_code']=collector.returncode
            finally:
                # Stop sudo/its owned runner first, allowing scoped Mininet finally cleanup.
                if collector is not None:
                    status['collector_exit_code']=stop_owned(collector,45)
                status['controller_stop_requested']=controller is not None and controller.poll() is None
                status['controller_exit_code']=stop_owned(controller,10)
    except BaseException as error:
        status['error']=f'{type(error).__name__}: {error}'
        status['interrupted']=True
    finally:
        status['model_unchanged']=file_hash(args.model)==model_hash
        status['source_unchanged']=all(file_hash(ROOT/f)==value for f,value in source_hashes.items())
        write_json(output/'trial-status.json',status)
    # Use the same read-only analysis/validation that will aggregate all four trials.
    sys.path.insert(0,str(ROOT/'docs/phase6c'))
    from analyze import validate_trial
    try:
        result=validate_trial(output)
        write_json(output/'validation.json',result)
        if result['errors']:raise RuntimeError('; '.join(result['errors']))
        status['status']='passed'
    except Exception as error:
        status['validation_error']=str(error)
        write_json(output/'validation.json',dict(errors=[str(error)]))
    write_json(output/'trial-status.json',status)
    print(json.dumps(dict(output=str(output),**status),indent=2))
    if status['status']!='passed':raise SystemExit(1)


if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
