"""Opt-in real Mininet/iperf3 telemetry-to-prediction acceptance experiment."""

import argparse
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mininet.log import setLogLevel
from mininet_topology import run_network
from telemetry_store import read_rows
from integration_phase2 import checks as phase2_checks, eventually, require


def check_network(net, args):
    phase2_checks(net)
    servers, clients, handles = [], [], []
    generator = random.Random(args.seed)
    try:
        for name in ('h4', 'h6'):
            handle = (args.log_dir / f'{name}-server.log').open('w')
            handles.append(handle)
            servers.append(net[name].popen(['iperf3', '-s', '-p', '5201'], stdout=handle, stderr=subprocess.STDOUT))
        time.sleep(1)
        require(all(p.poll() is None for p in servers), 'iperf3 server failed to start')
        elapsed = 0
        while elapsed < args.seconds:
            duration = min(generator.randint(20, 45), args.seconds - elapsed)
            # Mixed regimes; explicit high intervals create positive examples.
            rate = [10, 45, 95, generator.randint(10, 95)][(elapsed // 20) % 4]
            print(f'*** LIVE traffic seed={args.seed} elapsed={elapsed}s rate={rate}M duration={duration}s', flush=True)
            clients = []
            for src, dst in (('h1', 'h4'), ('h3', 'h6')):
                handle = (args.log_dir / f'{src}-{elapsed}.log').open('w')
                handles.append(handle)
                clients.append(net[src].popen(['iperf3', '-c', net[dst].IP(), '-u', '-b', f'{rate}M',
                    '-t', str(duration), '-i', '5'], stdout=handle, stderr=subprocess.STDOUT))
            for client in clients:
                require(client.wait(timeout=duration + 10) == 0, 'iperf3 client failed')
            clients = []
            elapsed += duration
        require(net.pingAll(timeout='2') == 0, 'Forwarding failed after traffic generation')
        observations = read_rows(args.database, 'observations')
        require(any(r['source'] == 'live_mininet' and r['valid'] and r['utilization'] >= 0.8
                    for r in observations), 'No genuinely measured congested examples; collect more varied traffic')
        result = subprocess.run([args.controller_python, 'phase3.py', 'train', str(args.database),
            '--model', str(args.model)], text=True, capture_output=True, timeout=60)
        require(result.returncode == 0, f'Training failed (do not lower evidence requirements): {result.stderr}')
        (args.log_dir / 'training.json').write_text(result.stdout)
        report = json.loads(result.stdout)
        require(report['train_positive_count'] > 0 and
                report['evaluation']['random_forest']['positive_event_count'] > 0,
                'Train/holdout lacks positive events; repeat with independent traffic sessions')
        def predictions_ready():
            return any(p['status'] == 'valid' and p['model_version'] == report['model_version']
                       for p in read_rows(args.database, 'predictions'))
        eventually(predictions_ready, 'controller loading the newly trained model and predicting', timeout=25)
        # Keep sampling while targets mature. This is not evidence of forecast skill.
        time.sleep(20)
        prediction_rows = [p for p in read_rows(args.database, 'predictions')
                           if p['status'] == 'valid' and p['model_version'] == report['model_version']]
        require(all(p['congestion_predicted'] == (p['predicted_utilization'] >= p['congestion_threshold'])
                    for p in prediction_rows), 'Incorrect congestion threshold classification')
        session = prediction_rows[-1]['session_id']
        result = subprocess.run([args.controller_python, 'phase3.py', 'evaluate', str(args.database),
            '--session', session], capture_output=True, text=True, timeout=20)
        require(result.returncode == 0, f'Matured prediction evaluation unavailable: {result.stderr}')
        (args.log_dir / 'matured-evaluation.json').write_text(result.stdout)
        require(net.pingAll(timeout='2') == 0, 'Phase 2 forwarding regressed during inference')
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller-ip', default='127.0.0.1')
    parser.add_argument('--controller-port', type=int, default=6653)
    parser.add_argument('--bandwidth-scale', type=float, default=0.1)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--controller-python', default='/home/bob_the_bob/.pyenv/versions/3.10.16/bin/python')
    parser.add_argument('--seconds', type=int, default=360)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--log-dir', type=Path, default=Path('/tmp/flowrider-phase3-live'))
    args = parser.parse_args()
    if args.seconds < 180:
        parser.error('--seconds must be >= 180; elapsed duration alone does not guarantee useful data')
    if not shutil.which('iperf3'):
        parser.error('iperf3 is required; this test will not install packages')
    args.database, args.model = args.database.resolve(), args.model.resolve()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    setLogLevel('info')
    run_network(args, checks=lambda net: check_network(net, args))
    from mininet_topology import preflight
    preflight(args.controller_ip, args.controller_port)
    print('*** LIVE PHASE 3 TELEMETRY-TO-PREDICTION AND CLEANUP CHECKS PASSED', flush=True)


if __name__ == '__main__':
    main()
