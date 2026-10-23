import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    from mininet_topology import FlowRiderTopo, check_tc_output, preflight, run_network
except ModuleNotFoundError as error:
    if error.name != 'mininet':
        raise
    FlowRiderTopo = None


@unittest.skipIf(FlowRiderTopo is None, 'Run with /usr/bin/python3 for Ubuntu Mininet')
class MininetTopologyTests(unittest.TestCase):
    def test_six_switches_hosts_and_exact_wiring(self):
        topo = FlowRiderTopo()
        self.assertEqual(topo.switches(), ['s1', 's2', 's3', 's4', 's5', 's6'])
        self.assertEqual(len(topo.hosts()), 6)
        self.assertEqual(len(topo.links()), 13)
        self.assertEqual(topo.port('s2', 's5'), (4, 4))
        self.assertEqual(topo.port('s1', 'h1'), (1, 0))
        self.assertEqual(topo.nodeInfo('s2')['protocols'], 'OpenFlow13')
        self.assertEqual(topo.nodeInfo('s2')['dpid'], '0000000000000002')
        self.assertEqual(topo.linkInfo('s2', 's5')['bw'], 200)
        self.assertEqual(topo.linkInfo('s2', 's5')['delay'], '2ms')

    def test_unsupported_bandwidth_is_rejected(self):
        for scale in (0, -1, 1, float('nan')):
            with self.assertRaises(ValueError):
                FlowRiderTopo(bandwidth_scale=scale)

    def test_tc_check_validates_applied_values(self):
        qdisc = 'qdisc netem 10: parent 5:1 limit 1000 delay 2ms'
        classes = 'class htb 5:1 root rate 200Mbit ceil 200Mbit burst 1Kb'
        check_tc_output(qdisc, classes, 200, 2)
        for q, c in [(qdisc, ''), ('qdisc noqueue', classes),
                     (qdisc, classes.replace('200Mbit', '100Mbit'))]:
            with self.assertRaises(RuntimeError):
                check_tc_output(q, c, 200, 2)

    def test_existing_bridge_is_rejected_before_any_network_creation(self):
        with patch('mininet_topology.os.geteuid', return_value=0), \
             patch('mininet_topology.command', side_effect=['s1\nother-user-network\n', '[]']), \
             patch('mininet_topology.socket.create_connection') as connect:
            with self.assertRaisesRegex(RuntimeError, 'refusing to replace'):
                preflight('127.0.0.1', 6653)
            connect.assert_not_called()

    def test_partial_startup_stops_only_owned_network(self):
        args = SimpleNamespace(controller_ip='127.0.0.1', controller_port=6653, bandwidth_scale=0.1)
        with patch('mininet_topology.preflight'), patch('mininet_topology.Mininet') as constructor:
            network = constructor.return_value
            network.start.side_effect = RuntimeError('startup failure')
            with self.assertRaisesRegex(RuntimeError, 'startup failure'):
                run_network(args)
            network.stop.assert_called_once_with()

    def test_failed_live_checks_still_stop_owned_network(self):
        args = SimpleNamespace(controller_ip='127.0.0.1', controller_port=6653, bandwidth_scale=0.1)
        with patch('mininet_topology.preflight'), patch('mininet_topology.verify_tc'), \
             patch('mininet_topology.Mininet') as constructor:
            network = constructor.return_value
            def failed_stage(net):
                self.assertIs(net, network)
                raise RuntimeError('iperf3 traffic stage 0 failed')
            with self.assertRaisesRegex(RuntimeError, 'traffic stage 0 failed'):
                run_network(args, checks=failed_stage)
            network.stop.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
