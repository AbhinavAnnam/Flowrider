"""Run feasibility checks and a controlled four-switch ABBA benchmark."""
import argparse
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
from phase6c import ORDER, create_trial, digest, make_plan, make_four_switch_plan, validate_plan, write_json


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


def run_benchmark(args):
    """One command, bounded child processes, immutable evidence, no global cleanup."""
    if args.topology!='phase6c':raise ValueError('Automatic benchmark uses the independent phase6c topology')
    if os.geteuid()!=0:
        try:subprocess.run(['sudo','-n','true'],check=True)
        except subprocess.CalledProcessError as error:
            raise RuntimeError('Authenticated sudo unavailable. Run: sudo /usr/bin/python3 scripts/run_phase6c.py --trials 4') from error
    python=controller_python(args.controller_python)
    subprocess.run([python,'-c','import os_ken, networkx, sklearn, matplotlib'],check=True)
    subprocess.run(['/usr/bin/python3','-c','import mininet'],check=True)
    for binary in ('ovs-vsctl','ovs-ofctl','tc','iperf3','ping','ip'):
        if shutil.which(binary) is None:raise RuntimeError(f'Missing required executable: {binary}')
    resources=resource_check()
    experiment=(args.experiment or ROOT/'data'/datetime.now(timezone.utc).strftime('phase6c-%Y%m%dT%H%M%S%fZ')).resolve()
    if experiment.exists():raise FileExistsError(f'Refusing to overwrite {experiment}')
    experiment.mkdir(mode=0o700,parents=True)
    write_json(experiment/'resources.json',resources)
    subprocess.run([python,str(ROOT/'scripts/check_phase6c_model.py'),'--model',str(args.model),
                    '--output',str(experiment/'model-compatibility.json')],check=True)
    common=['--model',str(args.model),'--seed',str(args.seed),'--queue-packets',str(args.queue_packets),
            '--controller-port',str(args.controller_port),'--controller-python',python,'--topology','phase6c']
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
            # Even failed trials produce a missing-data report; never substitute metrics.
            report=experiment/f'{name}-analysis'
            analysis=subprocess.run([python,str(ROOT/'docs/phase6c/analyze.py'),
                '--experiment',str(root),'--output',str(report)])
        if failure:raise failure
        if analysis.returncode:raise RuntimeError(f'Invalid comparison: {report}/report.md')
    if args.profile=='full':
        series('feasibility','feasibility',2)
        write_json(experiment/'resources-before-full.json',resource_check())
    series('trials',args.profile,args.trials)
    print(f'Completed benchmark: {experiment}/trials-analysis/report.md',flush=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',type=Path)
    action=parser.add_mutually_exclusive_group()
    action.add_argument('--init',action='store_true')
    action.add_argument('--trial',type=int,choices=range(1,5))
    action.add_argument('--trials',type=int,choices=(2,4))
    parser.add_argument('--profile',choices=['full','feasibility'],default='full')
    parser.add_argument('--topology',choices=['six','phase6c'],default='phase6c')
    parser.add_argument('--controller-python')
    parser.add_argument('--model',type=Path,default=ROOT/'data/research-20261015T180515Z/evaluation/model.pkl')
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--bottleneck-mbps',type=float,default=None,help='Legacy six-switch experiments only')
    parser.add_argument('--queue-packets',type=int,default=100)
    parser.add_argument('--controller-port',type=int,default=16653)
    args=parser.parse_args(argv);os.chdir(ROOT)
    args.model=args.model.resolve()
    if not args.init and args.trial is None:
        args.trials=args.trials or (2 if args.profile=='feasibility' else 4)
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
        plan=(make_four_switch_plan(args.seed,args.queue_packets,args.profile) if args.topology=='phase6c'
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
