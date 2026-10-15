import struct
import unittest
from types import SimpleNamespace as NS

from os_ken.controller.handler import DEAD_DISPATCHER
from os_ken.lib.packet import ethernet, packet
from os_ken.ofproto import ofproto_v1_3 as ofp, ofproto_v1_3_parser as parser

from controller import FlowRiderController, ROUTE_COOKIE
from topology_config import HOSTS, SWITCHES


class Datapath:
    """Use real OS-Ken message encoding; replace only the transport."""
    ofproto = ofp
    ofproto_parser = parser

    def __init__(self, dpid):
        self.id = dpid
        self.messages = []
        self.xid = 0
        self.closed = False

    def set_xid(self, message):
        self.xid += 1
        message.set_xid(self.xid)

    def send_msg(self, message):
        if message.xid is None:
            self.set_xid(message)
        message.serialize()
        self.messages.append(message)

    def close(self):
        self.closed = True


def event(dp, **fields):
    return NS(msg=NS(datapath=dp, **fields))


def frame(src=1, dst=4):
    pkt = packet.Packet()
    pkt.add_protocol(ethernet.ethernet(
        src=HOSTS[src]['mac'], dst=HOSTS[dst]['mac'] if isinstance(dst, int) else dst,
        ethertype=0x0806))
    pkt.serialize()
    return pkt.data


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.app = FlowRiderController()
        self.dps = {i: Datapath(i) for i in SWITCHES}

    def acknowledge(self):
        for (dpid, xid), dp in list(self.app.pending_barriers.items()):
            self.app.barrier_reply_handler(event(dp, xid=xid))

    def connect(self, dp, more=False):
        self.app.switch_features_handler(event(dp))
        ports = [NS(port_no=p, name=f's{dp.id}-eth{p}'.encode(), config=0, state=0)
                 for p in self.app.topology.expected_ports(dp.id)]
        self.app.port_desc_handler(event(dp, body=ports, flags=ofp.OFPMPF_REPLY_MORE if more else 0))

    def ready(self):
        for dp in self.dps.values():
            self.connect(dp)
        self.acknowledge()
        for dp in self.dps.values():
            dp.messages.clear()

    def packet_in(self, switch=1, ingress=1, src=1, dst=4, buffer=ofp.OFP_NO_BUFFER):
        self.app.packet_in_handler(event(
            self.dps[switch], match={'in_port': ingress}, data=frame(src, dst),
            buffer_id=buffer, msg_len=60, total_len=60))

    def test_features_registers_and_serializes_table_miss(self):
        dp = self.dps[1]
        self.connect(dp)
        self.assertIs(self.app.datapaths[1], dp)
        miss = next(m for m in dp.messages if isinstance(m, parser.OFPFlowMod) and m.priority == 0
                    and m.command == ofp.OFPFC_ADD)
        self.assertEqual(miss.instructions[0].actions[0].port, ofp.OFPP_CONTROLLER)
        self.assertEqual(miss.instructions[0].actions[0].max_len, ofp.OFPCML_NO_BUFFER)
        self.assertEqual(struct.unpack_from('!B', miss.buf)[0], ofp.OFP_VERSION)

    def test_unknown_switch_and_wrong_port_map_fail_closed(self):
        dp = Datapath(99)
        self.app.switch_features_handler(event(dp))
        self.assertTrue(dp.closed)
        self.connect(self.dps[1])
        self.app.port_desc_handler(event(self.dps[1], body=[NS(
            port_no=1, name=b'wrong', config=0, state=0)], flags=0))
        self.assertFalse(any(self.app.topology.port_up(1, p) for p in (1, 2, 3)))

    def test_known_unicast_rule_and_packet_out(self):
        self.ready()
        self.packet_in()
        dp = self.dps[1]
        rule = next(m for m in dp.messages if isinstance(m, parser.OFPFlowMod))
        self.assertEqual(rule.cookie, ROUTE_COOKIE)
        self.assertEqual(rule.match['in_port'], 1)
        self.assertEqual(rule.match['eth_src'], HOSTS[1]['mac'])
        self.assertEqual(rule.match['eth_dst'], HOSTS[4]['mac'])
        self.assertEqual(rule.instructions[0].actions[0].port, 2)
        self.assertGreater(rule.idle_timeout, 0)
        out = dp.messages[-1]
        self.assertIsInstance(out, parser.OFPPacketOut)
        self.assertEqual(out.actions[0].port, 2)
        self.assertIsNotNone(out.data)

    def test_buffered_packet_out_does_not_include_data(self):
        self.ready()
        self.packet_in(buffer=123)
        self.assertIsNone(self.dps[1].messages[-1].data)
        self.assertEqual(self.dps[1].messages[-1].buffer_id, 123)

    def test_unknown_and_broadcast_do_not_use_flood_or_install_flows(self):
        self.ready()
        for dst in ('ff:ff:ff:ff:ff:ff', '00:00:00:aa:bb:cc'):
            self.packet_in(dst=dst)
            messages = self.dps[1].messages
            self.assertEqual(len(messages), 1)
            self.assertIsInstance(messages[0], parser.OFPPacketOut)
            self.assertTrue(all(a.port < ofp.OFPP_MAX for a in messages[0].actions))
            messages.clear()

    def test_off_path_unicast_and_wrong_tree_ingress_are_dropped(self):
        self.ready()
        self.packet_in(switch=2, ingress=1)
        self.packet_in(switch=1, ingress=3, dst='ff:ff:ff:ff:ff:ff')
        self.assertEqual(self.dps[1].messages, [])
        self.assertEqual(self.dps[2].messages, [])

    def test_failure_deletes_owned_rules_and_waits_for_barriers(self):
        self.ready()
        self.app.port_status_handler(event(self.dps[2], reason=ofp.OFPPR_MODIFY,
            desc=NS(port_no=4, name=b's2-eth4', config=0, state=ofp.OFPPS_LINK_DOWN)))
        self.assertFalse(self.app.topology.graph[2][5]['active'])
        for dp in self.dps.values():
            delete = next(m for m in dp.messages if isinstance(m, parser.OFPFlowMod))
            self.assertEqual(delete.command, ofp.OFPFC_DELETE)
            self.assertEqual(delete.cookie, ROUTE_COOKIE)
            self.assertNotEqual(delete.cookie_mask, 0)
            self.assertIsInstance(dp.messages[-1], parser.OFPBarrierRequest)
        count = len(self.dps[1].messages)
        self.packet_in()
        self.assertEqual(len(self.dps[1].messages), count)
        self.acknowledge()
        self.assertFalse(self.app.pending_barriers)
        self.packet_in()
        self.assertIsInstance(self.dps[1].messages[-1], parser.OFPPacketOut)

    def test_disconnect_and_stale_connection_event(self):
        self.ready()
        old = self.dps[2]
        new = Datapath(2)
        self.connect(new)
        self.app.state_change_handler(NS(datapath=old, state=DEAD_DISPATCHER))
        self.assertIs(self.app.datapaths[2], new)
        self.app.state_change_handler(NS(datapath=new, state=DEAD_DISPATCHER))
        self.assertNotIn(2, self.app.datapaths)
        self.assertFalse(self.app.topology.graph[2][5]['active'])

    def test_multipart_ports_are_not_activated_early(self):
        self.connect(self.dps[1], more=True)
        self.assertFalse(self.app.topology.ports)
        self.app.port_desc_handler(event(self.dps[1], body=[], flags=0))
        self.assertTrue(self.app.topology.port_up(1, 1))

    def test_old_barrier_replies_do_not_release_new_update(self):
        self.ready()
        dp = self.dps[2]
        def status(up):
            self.app.port_status_handler(event(dp, reason=ofp.OFPPR_MODIFY,
                desc=NS(port_no=4, name=b's2-eth4', config=0,
                        state=0 if up else ofp.OFPPS_LINK_DOWN)))
        status(False)
        previous = list(self.app.pending_barriers.items())
        status(True)
        current = self.app.pending_barriers.copy()
        for (_, xid), source in previous:
            self.app.barrier_reply_handler(event(source, xid=xid))
        self.assertEqual(self.app.pending_barriers, current)
        self.acknowledge()
        self.assertFalse(self.app.pending_barriers)

    def test_no_path_is_logged_then_recovers_after_restoration(self):
        self.ready()
        for port in (2, 3):
            self.app.port_status_handler(event(self.dps[1], reason=ofp.OFPPR_MODIFY,
                desc=NS(port_no=port, name=f's1-eth{port}'.encode(), config=0,
                        state=ofp.OFPPS_LINK_DOWN)))
        self.acknowledge()
        self.dps[1].messages.clear()
        with self.assertLogs('FlowRiderController', level='WARNING'):
            self.packet_in()
        self.assertEqual(self.dps[1].messages, [])
        self.app.port_status_handler(event(self.dps[1], reason=ofp.OFPPR_MODIFY,
            desc=NS(port_no=2, name=b's1-eth2', config=0, state=0)))
        self.acknowledge()
        self.packet_in()
        self.assertIsInstance(self.dps[1].messages[-1], parser.OFPPacketOut)

    def test_port_delete_and_add_recover_configured_link(self):
        self.ready()
        for reason, active in ((ofp.OFPPR_DELETE, False), (ofp.OFPPR_ADD, True)):
            self.app.port_status_handler(event(self.dps[2], reason=reason,
                desc=NS(port_no=4, name=b's2-eth4', config=0, state=0)))
            self.assertEqual(self.app.topology.graph[2][5]['active'], active)

    def test_truncated_or_control_packets_do_not_install_routes(self):
        self.ready()
        for ethertype in (0x88cc, 0x86dd):
            pkt = packet.Packet()
            pkt.add_protocol(ethernet.ethernet(src=HOSTS[1]['mac'], dst=HOSTS[4]['mac'], ethertype=ethertype))
            pkt.serialize()
            self.app.packet_in_handler(event(self.dps[1], match={'in_port': 1},
                data=pkt.data, buffer_id=ofp.OFP_NO_BUFFER))
        self.app.packet_in_handler(event(self.dps[1], match={'in_port': 1},
            data=b'', buffer_id=ofp.OFP_NO_BUFFER))
        self.assertFalse(self.dps[1].messages)

    def test_protocol_errors_are_logged_and_disable_forwarding(self):
        self.ready()
        with self.assertLogs('FlowRiderController', level='ERROR'):
            self.app.error_handler(event(self.dps[1], type=1, code=2, data=b'bad flow'))
        self.assertNotIn(1, self.app.topology.switches)


if __name__ == '__main__':
    unittest.main()
