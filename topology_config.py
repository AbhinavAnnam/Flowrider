"""Shared lab wiring. DPID i = s<i> = Router_<i>; host ports are always 1.

This file uses only the standard library so Ubuntu's Mininet Python can use it
independently of the pyenv Python that runs OS-Ken and the simulation.
"""

import hashlib
import json
import math
import re

SWITCHES = {i: f's{i}' for i in range(1, 7)}
# (source DPID, destination DPID, logical capacity Mbps, one-way delay ms)
LINKS = (
    (1, 2, 1000, 5),
    (2, 3, 1000, 5),
    (3, 4, 1000, 5),
    (4, 5, 1000, 5),
    (5, 6, 1000, 5),
    (6, 1, 1000, 5),
    (2, 5, 2000, 2),
)
HOSTS = {
    i: dict(name=f'h{i}', switch=i, port=1, ip=f'10.0.0.{i}/24',
            mac=f'00:00:00:00:00:{i:02x}')
    for i in SWITCHES
}


def topology_definition(profile='six'):
    if profile == 'six':
        return SWITCHES, HOSTS, TOPOLOGY_VERSION
    if profile != 'phase6c':
        raise ValueError('Unknown topology profile')
    from experimental_topology import SWITCHES as switches, HOSTS as hosts, VERSION
    return switches, hosts, VERSION


def wired_links(profile='six'):
    """Return link dictionaries with deterministic, unique endpoint ports."""
    if profile == 'six':
        switches, hosts, links = SWITCHES, HOSTS, LINKS
    else:
        from experimental_topology import SWITCHES as switches, HOSTS as hosts, LINKS as links
        if profile != 'phase6c':raise ValueError('Unknown topology profile')
    occupied = {(h['switch'], h['port']) for h in hosts.values()}
    next_port = {i: 2 for i in switches}
    result = []
    for src, dst, capacity, delay in links:
        for endpoint in (src, dst):
            while (endpoint, next_port[endpoint]) in occupied:
                next_port[endpoint] += 1
        result.append(dict(src=src, dst=dst, src_port=next_port[src],
                           dst_port=next_port[dst], capacity=capacity,
                           base_delay=delay))
        next_port[src] += 1
        next_port[dst] += 1
    return result


# Identity of the shared logical wiring; bandwidth scale is recorded separately.
TOPOLOGY_VERSION = hashlib.sha256(json.dumps(dict(switches=SWITCHES,hosts=HOSTS,
    links=wired_links()),sort_keys=True,separators=(',',':')).encode()).hexdigest()[:16]


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
