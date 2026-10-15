"""Shared lab wiring. DPID i = s<i> = Router_<i>; host ports are always 1.

This file uses only the standard library so Ubuntu's Mininet Python can use it
independently of the pyenv Python that runs OS-Ken and the simulation.
"""

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


def wired_links():
    """Return link dictionaries with deterministic, unique endpoint ports."""
    next_port = {i: 2 for i in SWITCHES}
    result = []
    for src, dst, capacity, delay in LINKS:
        result.append(dict(src=src, dst=dst, src_port=next_port[src],
                           dst_port=next_port[dst], capacity=capacity,
                           base_delay=delay))
        next_port[src] += 1
        next_port[dst] += 1
    return result
