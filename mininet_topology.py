"""Root-only Mininet runner; --describe needs no privileges or network changes."""

import argparse
import json
import math
import os
import re
import socket
import subprocess

from mininet.cli import CLI
from mininet.link import TCIntf, TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

from topology_config import HOSTS, LINKS, SWITCHES, wired_links


class FlowRiderTopo(Topo):
    def build(self, bandwidth_scale=0.1):
        if not math.isfinite(bandwidth_scale) or not 0 < bandwidth_scale <= TCIntf.bwParamMax / max(l[2] for l in LINKS):
            raise ValueError('bandwidth scale must be > 0 and <= 0.5 (TCIntf maximum 1000 Mbps)')
        for dpid, name in SWITCHES.items():
            self.addSwitch(name, dpid=f'{dpid:016x}', protocols='OpenFlow13',
                           failMode='secure', inband=False)
        for host in HOSTS.values():
            self.addHost(host['name'], ip=host['ip'], mac=host['mac'])
            self.addLink(SWITCHES[host['switch']], host['name'],
                         port1=host['port'], port2=0, bw=1000 * bandwidth_scale)
        for link in wired_links():
            self.addLink(SWITCHES[link['src']], SWITCHES[link['dst']],
                         port1=link['src_port'], port2=link['dst_port'],
                         bw=link['capacity'] * bandwidth_scale,
                         delay=f"{link['base_delay']}ms", max_queue_size=1000)


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=15)


def check_tc_output(qdisc, classes, bandwidth, delay):
    """Check actual qdisc/class output, rather than trusting Mininet arguments."""
    rates = re.findall(r'\brate ([0-9.]+)([KMGT]?)bit\b', classes, re.IGNORECASE)
    multipliers = {'': 1, 'K': 1e3, 'M': 1e6, 'G': 1e9, 'T': 1e12}
    if 'htb' not in classes or not any(
            math.isclose(float(rate) * multipliers[unit.upper()], bandwidth * 1e6, rel_tol=0.01)
            for rate, unit in rates):
        raise RuntimeError(f'HTB rate {bandwidth} Mbps not applied: {classes}')
    if delay:
        delays = re.findall(r'\bdelay ([0-9.]+)(us|ms|s)\b', qdisc)
        if 'netem' not in qdisc or not any(
                math.isclose(float(value) * {'us': 0.001, 'ms': 1, 's': 1000}[unit],
                             delay, rel_tol=0.01) for value, unit in delays):
            raise RuntimeError(f'netem delay {delay} ms not applied: {qdisc}')


def verify_tc(bandwidth_scale):
    endpoints = []
    for link in wired_links():
        for end in ('src', 'dst'):
            interface = f"{SWITCHES[link[end]]}-eth{link[end + '_port']}"
            endpoints.append((interface, link['capacity'] * bandwidth_scale, link['base_delay']))
    for host in HOSTS.values():
        endpoints.append((f"{SWITCHES[host['switch']]}-eth1", 1000 * bandwidth_scale, 0))
    for interface, rate, delay in endpoints:
        qdisc = command('tc', 'qdisc', 'show', 'dev', interface)
        classes = command('tc', 'class', 'show', 'dev', interface)
        print(f'*** Applied TC {interface}:\n{qdisc}{classes}', flush=True)
        check_tc_output(qdisc, classes, rate, delay)


def preflight(controller_ip, controller_port):
    if os.geteuid() != 0:
        raise RuntimeError('Mininet needs root. Use sudo /usr/bin/python3 mininet_topology.py')
    existing = set(command('ovs-vsctl', 'list-br').split())
    collisions = existing.intersection(SWITCHES.values())
    interfaces = {item['ifname'] for item in json.loads(command('ip', '-j', 'link', 'show'))}
    collisions.update(name for name in interfaces if re.fullmatch(r's[1-6]-eth\d+|h[1-6]-eth0', name))
    if collisions:
        raise RuntimeError(f'Existing lab names {sorted(collisions)}; refusing to replace them. '
                           'Stop their owning Mininet session first. No global cleanup was run.')
    with socket.create_connection((controller_ip, controller_port), timeout=3):
        pass


def run_network(args, checks=None):
    preflight(args.controller_ip, args.controller_port)
    topo = FlowRiderTopo(bandwidth_scale=args.bandwidth_scale)
    # Build inside try so partial startup also stops only this network's objects.
    net = Mininet(topo=topo, switch=OVSSwitch, controller=None, link=TCLink,
                  build=False, autoStaticArp=False)
    try:
        net.addController('c0', controller=RemoteController,
                          ip=args.controller_ip, port=args.controller_port)
        net.start()
        if not net.waitConnected(timeout=20):
            raise RuntimeError('Not all six switches connected within 20 seconds')
        for host in net.hosts:
            # Phase 2 is IPv4/ARP only; suppress background IPv6 discovery.
            host.cmd('sysctl -qw net.ipv6.conf.all.disable_ipv6=1')
        verify_tc(args.bandwidth_scale)
        if checks:
            checks(net)
        else:
            CLI(net)
    finally:
        net.stop()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller-ip', default='127.0.0.1')
    parser.add_argument('--controller-port', type=int, default=6653)
    parser.add_argument('--bandwidth-scale', type=float, default=0.1)
    parser.add_argument('--describe', action='store_true')
    return parser.parse_args(argv)


def main():
    args = parse_args()
    if args.describe:
        FlowRiderTopo(bandwidth_scale=args.bandwidth_scale)  # validate configuration
        print(json.dumps(dict(switches=SWITCHES, hosts=HOSTS, links=wired_links(),
                              bandwidth_scale=args.bandwidth_scale), indent=2))
        return
    setLogLevel('info')
    try:
        run_network(args)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error


if __name__ == '__main__':
    main()
