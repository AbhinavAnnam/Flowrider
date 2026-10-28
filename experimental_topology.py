"""Opt-in Phase 6C wiring; the historical six-switch constants stay unchanged.

Logical capacities use the existing 0.1 lab scale. Delays are one-way per link.
H3 at S2 and H4 at S4 compete with H1→H2 across the S2→S4 bottleneck.
"""
import hashlib
import json

SWITCHES = {i: f's{i}' for i in range(1, 5)}
LINKS = ((1, 2, 1000, 2), (2, 4, 200, 2),
         (1, 3, 1000, 5), (3, 4, 1000, 5))
HOSTS = {i: dict(name=f'h{i}', switch=switch, port=port, ip=f'10.0.0.{i}/24',
                 mac=f'00:00:00:00:00:{i:02x}')
         for i, switch, port in ((1, 1, 1), (2, 4, 1), (3, 2, 1), (4, 4, 4))}
VERSION = hashlib.sha256(json.dumps(dict(switches=SWITCHES, hosts=HOSTS,
    links=LINKS), sort_keys=True).encode()).hexdigest()[:16]
