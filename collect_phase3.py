"""Collect real Mininet traffic in one independently seeded controller session.

Run with Ubuntu's system Python and sudo. The controller runs separately as the
normal user. No fitting, fabricated measurements, or global cleanup occurs here.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import shutil
import subprocess
import time

from telemetry_store import read_rows


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def traffic_schedule(seconds, seed):
    if seconds <= 0:
        raise ValueError('Duration must be positive')
    generator, schedule, elapsed = random.Random(seed), [], 0
    while elapsed < seconds:
        groups = [
            [('medium', generator.randint(35, 55), generator.randint(35, 55))],
            [('high', generator.randint(88, 95), generator.randint(45, 65))],
            [('ramp', rate, generator.randint(15, 22)) for rate in (20, 40, 60, 78, 93)],
            [('burst', rate, generator.randint(8, 15)) for rate in (8, 95, 12, 93, 8, 95)],
        ]
        generator.shuffle(groups)
        stages = [('low', generator.randint(5, 15), generator.randint(30, 45))]
        stages += [stage for group in groups for stage in group]
        stages += [('recovery', generator.randint(3, 12), generator.randint(30, 45))]
        reverse = generator.choice((False, True))
        for scenario, rate, duration in stages:
            duration = min(duration, seconds - elapsed)
            if duration <= 0:
                break
            schedule.append(dict(scenario=scenario, rate_mbps=rate, seconds=duration,
                                 reverse=reverse, planned_elapsed_seconds=elapsed))
            elapsed += duration
    return schedule


def validate_traffic_configuration(config,args):
    plan=config.get('traffic_configuration')
    if plan and plan.get('generator')=='collect_phase3':
        expected=dict(generator='collect_phase3',seconds=args.seconds,seed=args.seed,
            bandwidth_scale=args.bandwidth_scale,stages=traffic_schedule(args.seconds,args.seed),
            flow_pairs=[['h1','h4'],['h3','h6']],measurement_interval_seconds=5)
        require(plan==expected,'Traffic configuration differs from the recorded trial plan')


def collect(net, args):
    from phase3 import sessions
    available = sessions(args.database)
    require(len(available) == 1, 'Use a fresh database and controller for each independent session')
    session = available[0]
    config = json.loads(session['config_json'])
    validate_traffic_configuration(config,args)
    require(session['source'] == 'live_mininet', 'Controller must declare live_mininet provenance')
    require(config['bandwidth_scale'] == args.bandwidth_scale, 'Controller/Mininet capacities disagree')
    require(net.pingAll(timeout='2') == 0, 'Initial pingall failed')
    if args.phase2_checks:
        import sys
        sys.path.insert(0, str(Path(__file__).parent / 'tests'))
        from integration_phase2 import checks
        checks(net)
    servers, clients, handles, stages = [], [], [], []
    start = time.monotonic()
    try:
        for host in ('h1', 'h3', 'h4', 'h6'):
            handle = (args.output / f'{host}-server.log').open('w')
            handles.append(handle)
            servers.append(net[host].popen(['iperf3', '-s', '-p', '5201'], stdout=handle,
                                          stderr=subprocess.STDOUT))
        time.sleep(1)
        require(all(p.poll() is None for p in servers), 'An iperf3 server failed')
        for index, stage in enumerate(traffic_schedule(args.seconds, args.seed)):
            entry = dict(stage, started_timestamp=datetime.now(timezone.utc).isoformat(),
                         actual_elapsed_seconds=time.monotonic() - start, flows=[])
            stages.append(entry)
            clients = []
            for source, destination in (('h1', 'h4'), ('h3', 'h6')):
                if stage['reverse']:
                    source, destination = destination, source
                path = args.output / f'{index:03d}-{source}-{destination}.json'
                handle = path.open('w')
                handles.append(handle)
                clients.append(net[source].popen(['iperf3', '-c', net[destination].IP(),
                    '-u', '-b', f"{stage['rate_mbps']}M", '-t', str(stage['seconds']), '-i', '5',
                    '-J', '--get-server-output'], stdout=handle, stderr=subprocess.STDOUT))
                entry['flows'].append(dict(source=source, destination=destination, iperf_json=str(path)))
            for client in clients:
                require(client.wait(timeout=stage['seconds'] + 15) == 0, 'iperf3 flow failed; inspect its JSON')
            clients = []
            entry['finished_timestamp'] = datetime.now(timezone.utc).isoformat()
            print(json.dumps(entry), flush=True)
        # Let final predictions mature while genuine low background traffic is measured.
        time.sleep(20)
        require(net.pingAll(timeout='2') == 0, 'Forwarding failed after collection/inference')
    finally:
        for process in clients + servers:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        for handle in handles:
            handle.close()
        (args.output / 'traffic.json').write_text(json.dumps(dict(
            source='live_mininet', session_id=session['session_id'], seed=args.seed,
            routing_mode=config.get('routing_mode','legacy'),trial_id=config.get('trial_id',session['session_id']),
            topology_version=config.get('topology_version'),
            requested_traffic_seconds=args.seconds, elapsed_seconds=time.monotonic() - start,
            bandwidth_scale=args.bandwidth_scale, stages=stages), indent=2) + '\n')
    rows = read_rows(args.database, 'observations', session['session_id'])
    valid = [r for r in rows if r.get('valid')]
    require({r['link_id'] for r in valid} == {'s1-s2', 's2-s3', 's3-s4', 's4-s5', 's5-s6', 's1-s6', 's2-s5'},
            'Valid measurements do not cover all seven links')
    require(any(r['utilization'] >= 0.8 for r in valid), 'No measured congestion; inspect traffic and capacity')
    require(any(0.6 <= r['utilization'] < 0.8 for r in valid), 'No measured pre-congestion; collect more ramps')
    latest = max(datetime.fromisoformat(r['observation_timestamp'].replace('Z', '+00:00')) for r in valid)
    require((datetime.now(timezone.utc) - latest).total_seconds() < 10, 'Telemetry is stale')
    print(f"Collected session={session['session_id']} valid_link_samples={len(valid)}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--controller-ip', default='127.0.0.1')
    parser.add_argument('--controller-port', type=int, default=16653)
    parser.add_argument('--bandwidth-scale', type=float, default=0.1)
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--phase2-checks', action='store_true')
    args = parser.parse_args()
    if args.seconds < 420 or not shutil.which('iperf3'):
        parser.error('Require iperf3 and >=420 traffic seconds; duration alone does not ensure sufficient data')
    args.database = args.database.resolve()
    if not args.database.exists() or args.output.exists():
        parser.error('Require an existing controller database and a fresh output directory')
    args.output.mkdir(parents=True)
    from mininet.log import setLogLevel
    from mininet_topology import run_network
    setLogLevel('info')
    run_network(args, checks=lambda net: collect(net, args))


if __name__ == '__main__':
    main()
