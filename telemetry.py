"""Directional OpenFlow counter calculations, independent of OS-Ken and ML."""

import math

COUNTERS = ('tx_bytes', 'rx_bytes', 'tx_packets', 'rx_packets', 'tx_dropped',
            'rx_dropped', 'tx_errors', 'rx_errors')
UINT64_MAX = (1 << 64) - 1


def link_snapshot(topology, datapaths, scale=0.1):
    if not math.isfinite(scale) or not 0 < scale <= 0.5:
        raise ValueError('telemetry bandwidth scale must be in (0, 0.5]')
    links, ports = [], {}
    for u, v, edge in sorted(topology.graph.edges(data=True)):
        link_id = f's{min(u, v)}-s{max(u, v)}'
        links.append(dict(link_id=link_id, source_dpid=u, destination_dpid=v,
                          source_port=edge['ports'][u], destination_port=edge['ports'][v],
                          capacity_mbps=edge['capacity'] * scale, active=edge['active']))
        for dpid in (u, v):
            port = edge['ports'][dpid]
            ports[dpid, port] = dict(kind='inter_switch', link_id=link_id,
                capacity_mbps=edge['capacity'] * scale, active=edge['active'],
                token=id(datapaths.get(dpid)))
    for dpid, port in topology.access_ports:
        ports[dpid, port] = dict(kind='host', link_id=None, capacity_mbps=1000 * scale,
            active=topology.port_up(dpid, port), token=id(datapaths.get(dpid)))
    return links, ports


class PortSampler:
    def __init__(self, max_gap=10):
        self.previous = {}
        self.max_gap = max_gap

    def sample(self, key, counters, now, capacity, token, active=True, duration=None):
        values = {name: counters.get(name) if counters else None for name in COUNTERS}
        values = {k: v if isinstance(v, int) and 0 <= v < UINT64_MAX else None
                  for k, v in values.items()}
        result = dict(counters=values, monotonic_time=now, duration=duration,
                      status='initial', valid=False, tx_mbps=None, rx_mbps=None,
                      utilization=None, operational_utilization=None,
                      packet_drop_rate=None, port_error_rate=None)
        if not math.isfinite(now):
            result['status'] = 'invalid_elapsed'
            return result
        if not math.isfinite(capacity) or capacity <= 0:
            result['status'] = 'invalid_capacity'
            return result
        if not active or counters is None or values['tx_bytes'] is None or values['rx_bytes'] is None:
            self.previous.pop(key, None)
            result['status'] = 'inactive' if not active else 'missing'
            return result
        previous = self.previous.get(key)
        if previous and now <= previous['time']:
            result['status'] = 'invalid_elapsed'
            return result
        self.previous[key] = dict(time=now, values=values, token=token, duration=duration)
        if previous is None or token != previous['token']:
            return result
        if duration is not None and duration == previous['duration']:
            self.previous[key] = previous
            result['status'] = 'stale_counters'
            return result
        elapsed = now - previous['time']
        if not math.isfinite(elapsed) or elapsed <= 0:
            result['status'] = 'invalid_elapsed'
            return result
        if elapsed > self.max_gap:
            result['status'] = 'sampling_gap'
            return result
        reset = any(values[name] is not None and previous['values'][name] is not None
                    and values[name] < previous['values'][name] for name in COUNTERS)
        if duration is not None and previous['duration'] is not None and duration < previous['duration']:
            reset = True
        if reset:
            result['status'] = 'counter_reset'
            return result
        for direction in ('tx', 'rx'):
            name = direction + '_bytes'
            result[direction + '_mbps'] = 8 * (values[name] - previous['values'][name]) / elapsed / 1e6
        for suffix, output in (('dropped', 'packet_drop_rate'), ('errors', 'port_error_rate')):
            names = ['tx_' + suffix, 'rx_' + suffix]
            if all(values[n] is not None and previous['values'][n] is not None for n in names):
                result[output] = sum(values[n] - previous['values'][n] for n in names) / elapsed
        result['utilization'] = result['tx_mbps'] / capacity
        result['operational_utilization'] = min(1.0, max(0.0, result['utilization']))
        result['status'] = 'capacity_exceeded' if result['utilization'] > 1 else 'valid'
        result['valid'] = True
        return result


def aggregate_link(directions, active, max_skew=2):
    result = dict(status='inactive' if not active else 'invalid_directions', valid=False,
                  utilization=None, operational_utilization=None, tx_mbps=None, rx_mbps=None)
    if not active or len(directions) != 2 or not all(d['valid'] for d in directions):
        return result
    if abs(directions[0]['monotonic_time'] - directions[1]['monotonic_time']) > max_skew:
        result['status'] = 'endpoint_skew'
        return result
    busiest = max(directions, key=lambda d: d['utilization'])
    result.update(utilization=busiest['utilization'], tx_mbps=busiest['tx_mbps'], rx_mbps=busiest['rx_mbps'],
                  operational_utilization=min(1.0, max(0.0, busiest['utilization'])), valid=True,
                  status='capacity_exceeded' if busiest['utilization'] > 1 else 'valid')
    return result
