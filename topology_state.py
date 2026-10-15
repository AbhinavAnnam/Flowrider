"""Static wiring plus observed endpoint state; no telemetry or ML dependencies."""

import networkx as nx

from network_topology import calculate_shortest_path
from topology_config import HOSTS, SWITCHES, wired_links


class TopologyState:
    def __init__(self):
        self.graph = nx.Graph()
        self.graph.add_nodes_from(
            (i, dict(switch=name, router=f'Router_{i}'))
            for i, name in SWITCHES.items()
        )
        self.switches = set()
        self.routing_weight = 'base_delay'
        self.ports = {}
        self.link_ports = {}
        self.access_ports = {(h['switch'], h['port']) for h in HOSTS.values()}
        self.static_hosts = {h['mac']: (h['switch'], h['port']) for h in HOSTS.values()}
        self.hosts = self.static_hosts.copy()
        for link in wired_links():
            u, v = link['src'], link['dst']
            p, q = link['src_port'], link['dst_port']
            self.link_ports[u, p] = (v, q)
            self.link_ports[v, q] = (u, p)
            self.graph.add_edge(
                u, v, ports={u: p, v: q}, capacity=link['capacity'],
                base_delay=link['base_delay'], latency=link['base_delay'],
                current_load=0, active=False, configured=True,
            )

    def expected_ports(self, dpid):
        return {p for switch, p in self.link_ports.keys() | self.access_ports
                if switch == dpid}

    def register_switch(self, dpid):
        if dpid not in SWITCHES:
            raise ValueError(f'Unconfigured DPID {dpid}')
        self.switches.add(dpid)

    def unregister_switch(self, dpid):
        self.switches.discard(dpid)
        self.ports = {k: v for k, v in self.ports.items() if k[0] != dpid}
        self._refresh_links()

    def _validate_port(self, dpid, port, name):
        if port not in self.expected_ports(dpid) or name != f'{SWITCHES[dpid]}-eth{port}':
            raise ValueError(f'Unexpected port {dpid}/{port}: {name!r}')

    def replace_ports(self, dpid, ports):
        for port, (name, up) in ports.items():
            self._validate_port(dpid, port, name)
        self.ports = {k: v for k, v in self.ports.items() if k[0] != dpid}
        self.ports.update({(dpid, port): up for port, (_, up) in ports.items()})
        self._refresh_links()

    def set_port(self, dpid, port, name, up):
        self._validate_port(dpid, port, name)
        changed = self.ports.get((dpid, port)) != up
        self.ports[dpid, port] = up
        self._refresh_links()
        return changed

    def port_up(self, dpid, port):
        return dpid in self.switches and self.ports.get((dpid, port), False)

    def _refresh_links(self):
        for u, v, edge in self.graph.edges(data=True):
            edge['active'] = all(self.port_up(dpid, edge['ports'][dpid]) for dpid in (u, v))

    def active_graph(self):
        graph = self.graph.subgraph(sorted(self.switches)).copy()
        graph.remove_edges_from([(u, v) for u, v, d in graph.edges(data=True) if not d['active']])
        return graph

    def path(self, src, dst):
        graph = self.active_graph()
        if src not in graph or dst not in graph:
            return None
        route = calculate_shortest_path(graph, src, dst, weight_metric=self.routing_weight)
        return route['path'] if route else None

    def learn_host(self, mac, dpid, port):
        """Learn only on access ports; never mistake a transit port for a host."""
        attachment = (dpid, port)
        if attachment not in self.access_ports or not self.port_up(dpid, port):
            return False
        if mac in self.static_hosts and self.static_hosts[mac] != attachment:
            return False
        changed = self.hosts.get(mac) != attachment
        self.hosts[mac] = attachment
        return changed

    def output_port(self, dpid, dst):
        attachment = self.hosts.get(dst)
        if attachment is None or not self.port_up(*attachment):
            return None
        target, host_port = attachment
        path = self.path(dpid, target)
        if not path:
            return None
        return host_port if len(path) == 1 else self.graph[dpid][path[1]]['ports'][dpid]

    def broadcast_tree(self, root):
        """Deterministic shortest-path tree rooted at the source attachment."""
        graph = self.active_graph()
        tree = nx.DiGraph()
        if root in graph:
            paths = nx.single_source_dijkstra_path(graph, root, weight='base_delay')
            tree.add_node(root)
            for path in paths.values():
                tree.add_edges_from(zip(path, path[1:]))
        return tree

    def broadcast_ports(self, dpid, ingress, src):
        attachment = self.hosts.get(src)
        if attachment is None or not self.port_up(*attachment):
            return []
        root, host_port = attachment
        tree = self.broadcast_tree(root)
        if dpid not in tree:
            return []
        if dpid == root:
            expected = host_port
        else:
            parent = next(tree.predecessors(dpid))
            expected = self.graph[dpid][parent]['ports'][dpid]
        if ingress != expected or not self.port_up(dpid, ingress):
            return []
        outputs = [self.graph[dpid][child]['ports'][dpid] for child in tree.successors(dpid)]
        outputs += [p for switch, p in sorted(self.access_ports)
                    if switch == dpid and p != ingress and self.port_up(switch, p)]
        return sorted(outputs)
