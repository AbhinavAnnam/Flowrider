import unittest

import networkx as nx

from topology_config import HOSTS, LINKS, SWITCHES
from topology_state import TopologyState
from network_topology import build_network_graph, calculate_shortest_path


def activate(state):
    for dpid, switch in SWITCHES.items():
        state.register_switch(dpid)
        state.replace_ports(dpid, {
            port: (f"{switch}-eth{port}", True)
            for port in state.expected_ports(dpid)
        })


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.state = TopologyState()

    def test_configured_graph_and_unique_port_mapping(self):
        self.assertEqual(len(SWITCHES), 6)
        self.assertEqual(len(LINKS), 7)
        self.assertEqual(self.state.graph.number_of_edges(), 7)
        self.assertEqual(self.state.link_ports[2, 4], (5, 4))
        self.assertEqual(self.state.link_ports[5, 4], (2, 4))
        self.assertEqual(self.state.graph[2][5]['base_delay'], 2)
        self.assertFalse(self.state.active_graph().edges)

    def test_both_endpoints_required_and_port_names_validated(self):
        self.state.register_switch(1)
        self.state.replace_ports(1, {2: ('s1-eth2', True)})
        self.assertFalse(self.state.graph[1][2]['active'])
        self.state.register_switch(2)
        with self.assertRaises(ValueError):
            self.state.replace_ports(2, {2: ('wrong-eth2', True)})
        self.state.replace_ports(2, {2: ('s2-eth2', True)})
        self.assertTrue(self.state.graph[1][2]['active'])

    def test_disconnection_removes_routes_and_ports(self):
        activate(self.state)
        self.state.unregister_switch(2)
        self.assertNotIn(2, self.state.active_graph())
        self.assertFalse(self.state.graph[2][5]['active'])
        self.assertNotIn((2, 4), self.state.ports)

    def test_static_host_registration_and_access_only_learning(self):
        activate(self.state)
        self.assertEqual(self.state.hosts[HOSTS[4]['mac']], (4, 1))
        self.assertFalse(self.state.learn_host('00:00:00:aa:bb:cc', 2, 4))
        self.assertTrue(self.state.learn_host('00:00:00:aa:bb:cc', 2, 1))
        self.assertEqual(self.state.hosts['00:00:00:aa:bb:cc'], (2, 1))
        self.assertFalse(self.state.learn_host(HOSTS[4]['mac'], 2, 1))

    def test_delay_weighted_path_and_explicit_output(self):
        activate(self.state)
        self.assertEqual(self.state.path(1, 4), [1, 2, 5, 4])
        self.assertEqual(self.state.output_port(1, HOSTS[4]['mac']), 2)
        self.assertEqual(self.state.output_port(4, HOSTS[4]['mac']), 1)
        self.assertIsNone(self.state.output_port(1, '00:00:00:aa:bb:cc'))

    def test_failure_reroute_partition_and_recovery(self):
        activate(self.state)
        self.state.set_port(2, 4, 's2-eth4', False)
        self.assertNotEqual(self.state.path(1, 4), [1, 2, 5, 4])
        self.assertNotIn((2, 5), list(zip(self.state.path(1, 4), self.state.path(1, 4)[1:])))
        self.state.set_port(1, 2, 's1-eth2', False)
        self.state.set_port(1, 3, 's1-eth3', False)
        self.assertIsNone(self.state.path(1, 4))
        self.assertIsNone(self.state.output_port(1, HOSTS[4]['mac']))
        self.state.set_port(1, 2, 's1-eth2', True)
        self.state.set_port(1, 3, 's1-eth3', True)
        self.state.set_port(2, 4, 's2-eth4', True)
        self.assertEqual(self.state.path(1, 4), [1, 2, 5, 4])

    def test_access_port_failure_blocks_host_delivery(self):
        activate(self.state)
        self.state.set_port(4, 1, 's4-eth1', False)
        self.assertIsNone(self.state.output_port(1, HOSTS[4]['mac']))

    def test_unknown_and_broadcast_traverse_tree_exactly_once(self):
        activate(self.state)
        src = HOSTS[1]['mac']
        queue = [(1, 1)]
        visited = []
        delivered = []
        while queue:
            switch, ingress = queue.pop(0)
            self.assertNotIn(switch, visited, 'broadcast loop or duplicate delivery')
            visited.append(switch)
            for port in self.state.broadcast_ports(switch, ingress, src):
                if (switch, port) in self.state.link_ports:
                    queue.append(self.state.link_ports[switch, port])
                else:
                    delivered.append(switch)
        self.assertEqual(set(visited), set(SWITCHES))
        self.assertEqual(set(delivered), {2, 3, 4, 5, 6})
        self.assertEqual(len(delivered), 5)
        self.assertEqual(self.state.broadcast_ports(1, 3, src), [])
        self.assertEqual(self.state.broadcast_ports(2, 1, src), [])

    def test_broadcast_forest_after_partition_and_restoration(self):
        activate(self.state)
        for port in (2, 3):
            self.state.set_port(1, port, f's1-eth{port}', False)
        self.assertEqual(self.state.broadcast_ports(1, 1, HOSTS[1]['mac']), [])
        self.assertTrue(nx.is_forest(self.state.broadcast_tree(2)))
        self.state.set_port(1, 2, 's1-eth2', True)
        self.assertTrue(self.state.broadcast_ports(1, 1, HOSTS[1]['mac']))

    def test_shared_graph_preserves_simulation_metrics_and_path(self):
        simulation = build_network_graph()
        self.assertEqual(simulation.number_of_nodes(), 6)
        for u, v, capacity, delay in LINKS:
            data = simulation[f'Router_{u}'][f'Router_{v}']
            self.assertEqual((data['capacity'], data['base_delay']), (capacity, delay))
        route = calculate_shortest_path(simulation, 'Router_1', 'Router_4')
        self.assertEqual(route['path'], ['Router_1', 'Router_2', 'Router_5', 'Router_4'])
        self.assertEqual(route['total_latency_ms'], 12)
        self.assertEqual(route['bottleneck_capacity_mbps'], 1000)

    def test_broadcast_has_no_duplicates_for_every_source_and_failure_combination(self):
        # All 128 up/down combinations of the seven physical links.
        for mask in range(1 << len(LINKS)):
            state = TopologyState()
            activate(state)
            for index, (u, v, _, _) in enumerate(LINKS):
                if mask & (1 << index):
                    port = state.graph[u][v]['ports'][u]
                    state.set_port(u, port, f's{u}-eth{port}', False)
            for root in SWITCHES:
                with self.subTest(mask=mask, root=root):
                    queue, visited = [(root, 1)], set()
                    while queue:
                        switch, ingress = queue.pop(0)
                        self.assertNotIn(switch, visited)
                        visited.add(switch)
                        for port in state.broadcast_ports(switch, ingress, HOSTS[root]['mac']):
                            peer = state.link_ports.get((switch, port))
                            if peer:
                                queue.append(peer)
                    self.assertEqual(visited, nx.node_connected_component(state.active_graph(), root))


if __name__ == '__main__':
    unittest.main()
