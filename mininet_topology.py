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

from topology_config import HOSTS, LINKS, SWITCHES, wired_links, check_tc_output, topology_definition


class FlowRiderTopo(Topo):
    def build(self, bandwidth_scale=0.1, profile='six', queue_packets=100):
        switches, hosts, _ = topology_definition(profile)
        if not math.isfinite(bandwidth_scale) or not 0 < bandwidth_scale <= TCIntf.bwParamMax / max(l[2] for l in LINKS):
            raise ValueError('bandwidth scale must be > 0 and <= 0.5 (TCIntf maximum 1000 Mbps)')
        for dpid, name in switches.items():
            self.addSwitch(name, dpid=f'{dpid:016x}', protocols='OpenFlow13',
                           failMode='secure', inband=False)
        for host in hosts.values():
            self.addHost(host['name'], ip=host['ip'], mac=host['mac'])
            self.addLink(switches[host['switch']], host['name'],
                         port1=host['port'], port2=0, bw=1000 * bandwidth_scale)
        for link in wired_links(profile):
            self.addLink(switches[link['src']], switches[link['dst']],
                         port1=link['src_port'], port2=link['dst_port'],
                         bw=link['capacity'] * bandwidth_scale,
                         delay=f"{link['base_delay']}ms", max_queue_size=(
                             queue_packets if profile=='phase6c' and {link['src'],link['dst']}=={2,4} else 1000))


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=15)


def verify_tc(bandwidth_scale, profile='six'):
    switches, hosts, _ = topology_definition(profile)
    endpoints = []
    for link in wired_links(profile):
        for end in ('src', 'dst'):
            interface = f"{switches[link[end]]}-eth{link[end + '_port']}"
            endpoints.append((interface, link['capacity'] * bandwidth_scale, link['base_delay']))
    for host in hosts.values():
        endpoints.append((f"{switches[host['switch']]}-eth{host['port']}", 1000 * bandwidth_scale, 0))
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
    profile = getattr(args, 'topology', 'six')
    topo = FlowRiderTopo(bandwidth_scale=args.bandwidth_scale, profile=profile,
                         queue_packets=getattr(args, 'queue_packets', 100))
    # Build inside try so partial startup also stops only this network's objects.
    net = Mininet(topo=topo, switch=OVSSwitch, controller=None, link=TCLink,
                  build=False, autoStaticArp=False)
    try:
        net.addController('c0', controller=RemoteController,
                          ip=args.controller_ip, port=args.controller_port)
        net.start()
        if not net.waitConnected(timeout=20):
            raise RuntimeError('Not all selected switches connected within 20 seconds')
        for host in net.hosts:
            # Phase 2 is IPv4/ARP only; suppress background IPv6 discovery.
            host.cmd('sysctl -qw net.ipv6.conf.all.disable_ipv6=1')
        verify_tc(args.bandwidth_scale, profile)
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
    parser.add_argument('--topology', choices=['six','phase6c'], default='six')
    return parser.parse_args(argv)


def main():
    args = parse_args()
    if args.describe:
        FlowRiderTopo(bandwidth_scale=args.bandwidth_scale, profile=args.topology)
        switches, hosts, _ = topology_definition(args.topology)
        print(json.dumps(dict(switches=switches, hosts=hosts, links=wired_links(args.topology),
                              bandwidth_scale=args.bandwidth_scale), indent=2))
        return
    setLogLevel('info')
    try:
        run_network(args)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error


if __name__ == '__main__':
    main()
