"""Opt-in LIVE Mininet test. Requires sudo, OVS, and an external controller."""

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mininet.log import setLogLevel
from mininet_topology import command, preflight, run_network
from topology_config import HOSTS, SWITCHES


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def eventually(check, description, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.5)
    raise RuntimeError(f'Timed out: {description}')


def ping(net, src, dst, count=3):
    output, error, status = net[src].pexec('ping', '-n', '-c', str(count), '-W', '2', net[dst].IP())
    print(f'*** {src} -> {dst}\n{output}{error}', flush=True)
    return status == 0 and f'{count} received' in output and 'DUP!' not in output


def flows(switch):
    output = command('ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', switch)
    print(f'*** {switch} flows\n{output}', flush=True)
    return output


def has_route(switch, src, dst, port):
    return any('cookie=0x4652' in line and 'priority=10' in line
               and f"dl_src={HOSTS[src]['mac']}" in line
               and f"dl_dst={HOSTS[dst]['mac']}" in line
               and f'actions=output:{port}' in line
               for line in flows(switch).splitlines())


def checks(net):
    print(command('ovs-vsctl', 'show'), flush=True)
    for dpid, name in SWITCHES.items():
        description = command('ovs-ofctl', '-O', 'OpenFlow13', 'show', name)
        print(description, flush=True)
        require('(OF1.3)' in description and f'dpid:{dpid:016x}' in description,
                f'{name} did not report expected DPID and OpenFlow 1.3')
        eventually(lambda: 'priority=0 actions=CONTROLLER:65535' in flows(name), f'{name} table miss')
    # No static ARP: initial ping must exercise broadcast ARP across the cyclic graph.
    eventually(lambda: ping(net, 'h2', 'h5'), 'initial ARP and host connectivity')
    require(net.pingAll(timeout='2') == 0, 'pingall reported loss')
    for host in net.hosts:
        neighbors = host.cmd('ip -4 neigh show')
        print(f'*** {host.name} ARP\n{neighbors}', flush=True)
        require('lladdr' in neighbors and 'INCOMPLETE' not in neighbors and 'FAILED' not in neighbors,
                f'{host.name} ARP resolution failed')
    eventually(lambda: has_route('s2', 2, 5, 4), 'base-delay backbone route on s2 port 4')
    require(any(re.search(r'n_packets=[1-9]\d*', line) for line in flows('s2').splitlines()
                if 'cookie=0x4652' in line), 'unicast rules have no actual packet hits')

    down = []
    try:
        print('*** FAIL backbone s2-s5', flush=True)
        net.configLinkStatus('s2', 's5', 'down')
        down.append(('s2', 's5'))
        eventually(lambda: ping(net, 'h2', 'h5'), 'reroute around failed backbone')
        eventually(lambda: not has_route('s2', 2, 5, 4), 'obsolete backbone route removed')
        require(net.pingAll(timeout='2') == 0, 'pingall failed after backbone failure')

        print('*** RESTORE backbone s2-s5', flush=True)
        net.configLinkStatus('s2', 's5', 'up')
        down.remove(('s2', 's5'))
        eventually(lambda: ping(net, 'h2', 'h5') and has_route('s2', 2, 5, 4), 'backbone recovery')

        # Isolate h1: old ARP/cache and route state must not bypass the partition.
        for edge in (('s1', 's2'), ('s1', 's6')):
            net.configLinkStatus(*edge, 'down')
            down.append(edge)
        eventually(lambda: not ping(net, 'h1', 'h4', count=1), 'safe no-path behavior')
        for edge in list(down):
            net.configLinkStatus(*edge, 'up')
            down.remove(edge)
        # Flush neighbor caches to test the recovered broadcast tree as well.
        for host in net.hosts:
            host.cmd('ip -4 neigh flush all')
        eventually(lambda: ping(net, 'h1', 'h4'), 'ARP and forwarding after partition recovery')
        require(net.pingAll(timeout='2') == 0, 'final pingall failed')
    finally:
        for edge in down:
            net.configLinkStatus(*edge, 'up')
    print('*** LIVE PHASE 2 NETWORK CHECKS PASSED', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller-ip', default='127.0.0.1')
    parser.add_argument('--controller-port', type=int, default=6653)
    parser.add_argument('--bandwidth-scale', type=float, default=0.1)
    parser.add_argument('--repeat', type=int, default=2)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error('--repeat must be positive')
    setLogLevel('info')
    for iteration in range(args.repeat):
        print(f'*** LIVE RUN {iteration + 1}/{args.repeat}', flush=True)
        run_network(args, checks=checks)
        # Preflight after stop verifies scoped cleanup and readiness for repeat.
        preflight(args.controller_ip, args.controller_port)
    print('*** LIVE PHASE 2 PASSED, INCLUDING CLEANUP AND REPEATED EXECUTION', flush=True)


if __name__ == '__main__':
    main()
