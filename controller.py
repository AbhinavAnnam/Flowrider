"""OS-Ken/OpenFlow 1.3 forwarding for the fixed FlowRider Phase 2 lab."""

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls,
)
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet, ethernet, ether_types

from topology_state import TopologyState


ROUTE_COOKIE = 0x4652000000000000
COOKIE_MASK = 0xFFFF000000000000


class FlowRiderController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(FlowRiderController, self).__init__(*args, **kwargs)
        self.topology = TopologyState()
        self.datapaths = {}
        self.mac_to_port = {}
        self.pending_barriers = {}
        self.validated = set()
        self.failed = set()
        self.port_snapshots = {}
        self.port_overrides = {}

    def _current(self, datapath):
        return self.datapaths.get(datapath.id) is datapath

    def _invalidate_routes(self):
        """Pause PacketIn forwarding until every healthy switch confirms deletes.

        The table-miss remains in place. Cookies restrict deletion to our routes.
        Barrier replies from previous connections/updates cannot release the pause.
        """
        self.pending_barriers.clear()
        for dpid, dp in self.datapaths.items():
            if dpid in self.failed:
                continue
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, cookie=ROUTE_COOKIE, cookie_mask=COOKIE_MASK,
                table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                match=parser.OFPMatch(),
            ))
            barrier = parser.OFPBarrierRequest(dp)
            dp.set_xid(barrier)
            self.pending_barriers[dpid, barrier.xid] = dp
            dp.send_msg(barrier)
        self.logger.info('Topology changed; invalidating routes on %d switches',
                         len(self.pending_barriers))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        try:
            self.topology.unregister_switch(dp.id)
            self.topology.register_switch(dp.id)
        except ValueError as error:
            self.logger.error('%s; rejecting connection', error)
            dp.close()
            return
        self.datapaths[dp.id] = dp
        self.failed.discard(dp.id)
        self.validated.discard(dp.id)
        self.mac_to_port[dp.id] = {}
        self.port_snapshots[dp.id] = {}
        self.port_overrides[dp.id] = {}
        ofp, parser = dp.ofproto, dp.ofproto_parser
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, priority=0, match=parser.OFPMatch(),
            instructions=[parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)],
        ))
        self._invalidate_routes()
        # OS-Ken's built-in OFPHandler requests the port descriptions during
        # this handshake. Avoid a second snapshot that could race PortStatus.
        self.logger.info('Switch connected: DPID=%d OpenFlow=1.3', dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state != DEAD_DISPATCHER or not self._current(dp):
            return
        del self.datapaths[dp.id]
        self.topology.unregister_switch(dp.id)
        self.validated.discard(dp.id)
        self.failed.discard(dp.id)
        self.mac_to_port.pop(dp.id, None)
        self.port_snapshots.pop(dp.id, None)
        self.port_overrides.pop(dp.id, None)
        self._invalidate_routes()
        self.logger.info('Switch disconnected: DPID=%d', dp.id)

    @staticmethod
    def _port_description(port, ofp):
        name = port.name.decode('utf-8').rstrip('\x00') if isinstance(port.name, bytes) else port.name
        up = not (port.config & ofp.OFPPC_PORT_DOWN or
                  port.state & (ofp.OFPPS_LINK_DOWN | ofp.OFPPS_BLOCKED))
        return name, up

    def _reject_ports(self, dp, error):
        self.validated.discard(dp.id)
        self.topology.replace_ports(dp.id, {})
        self._invalidate_routes()
        self.logger.error('Invalid static wiring on DPID=%d: %s; forwarding disabled', dp.id, error)

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def port_desc_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        if not self._current(dp) or dp.id in self.failed:
            return
        ports = self.port_snapshots.setdefault(dp.id, {})
        for port in msg.body:
            if port.port_no < dp.ofproto.OFPP_MAX:
                ports[port.port_no] = self._port_description(port, dp.ofproto)
        if msg.flags & dp.ofproto.OFPMPF_REPLY_MORE:
            return
        ports.update(self.port_overrides.pop(dp.id, {}))
        self.port_snapshots.pop(dp.id, None)
        try:
            if set(ports) != self.topology.expected_ports(dp.id):
                raise ValueError(f'expected ports {sorted(self.topology.expected_ports(dp.id))}, '
                                 f'received {sorted(ports)}')
            self.topology.replace_ports(dp.id, ports)
        except ValueError as error:
            self._reject_ports(dp, error)
            return
        self.validated.add(dp.id)
        self._invalidate_routes()
        self.logger.info('Validated static port map: DPID=%d ports=%s', dp.id, sorted(ports))

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        if not self._current(dp) or dp.id in self.failed or msg.desc.port_no >= dp.ofproto.OFPP_MAX:
            return
        port = msg.desc.port_no
        name, up = self._port_description(msg.desc, dp.ofproto)
        up = up and msg.reason != dp.ofproto.OFPPR_DELETE
        if dp.id in self.port_snapshots:
            self.port_overrides.setdefault(dp.id, {})[port] = (name, up)
        if dp.id not in self.validated:
            return
        try:
            changed = self.topology.set_port(dp.id, port, name, up)
        except ValueError as error:
            self._reject_ports(dp, error)
            return
        if changed:
            self.logger.info('Port status: DPID=%d port=%d up=%s (configured endpoint state)',
                             dp.id, port, up)
            self._invalidate_routes()

    @set_ev_cls(ofp_event.EventOFPBarrierReply, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def barrier_reply_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        key = (dp.id, msg.xid)
        if self._current(dp) and self.pending_barriers.get(key) is dp:
            del self.pending_barriers[key]
            if not self.pending_barriers:
                self.logger.info('Forwarding ready: switches=%s active_links=%d',
                                 sorted(self.validated), self.topology.active_graph().number_of_edges())

    @set_ev_cls(ofp_event.EventOFPErrorMsg, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def error_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        if not self._current(dp):
            return
        self.logger.error('OpenFlow error: DPID=%d type=%d code=%d data=%r; reconnect required',
                          dp.id, msg.type, msg.code, msg.data)
        self.failed.add(dp.id)
        self.validated.discard(dp.id)
        self.topology.unregister_switch(dp.id)
        self._invalidate_routes()
        dp.close()

    def _unicast_port(self, dpid, ingress, src, dst):
        source, target = self.topology.hosts.get(src), self.topology.hosts.get(dst)
        if source is None or target is None or not self.topology.port_up(*target):
            return None
        if not self.topology.port_up(*source):
            return None
        path = self.topology.path(source[0], target[0])
        if not path or dpid not in path:
            return None
        index = path.index(dpid)
        expected = source[1] if index == 0 else self.topology.graph[dpid][path[index - 1]]['ports'][dpid]
        if ingress != expected:
            return None
        return target[1] if index == len(path) - 1 else self.topology.graph[dpid][path[index + 1]]['ports'][dpid]

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        if not self._current(dp) or dp.id not in self.validated or self.pending_barriers:
            return
        ingress = msg.match['in_port']
        if not self.topology.port_up(dp.id, ingress):
            return
        eth = packet.Packet(msg.data).get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return
        if int(eth.src.split(':')[0], 16) & 1:
            return
        if (dp.id, ingress) in self.topology.access_ports:
            if eth.src in self.topology.static_hosts and self.topology.static_hosts[eth.src] != (dp.id, ingress):
                self.logger.warning('Unexpected host attachment: %s on %d/%d', eth.src, dp.id, ingress)
                return
            changed = self.topology.learn_host(eth.src, dp.id, ingress)
            self.mac_to_port.setdefault(dp.id, {})[eth.src] = ingress
            if changed:
                self._invalidate_routes()
                return  # Sender retries after the deletion barriers.
        ofp, parser = dp.ofproto, dp.ofproto_parser
        known_unicast = not int(eth.dst.split(':')[0], 16) & 1 and eth.dst in self.topology.hosts
        if known_unicast:
            out_port = self._unicast_port(dp.id, ingress, eth.src, eth.dst)
            if out_port is None or out_port == ingress:
                self.logger.warning('No valid path/ingress: %s -> %s at DPID=%d port=%d',
                                    eth.src, eth.dst, dp.id, ingress)
                return
            outputs = [out_port]
        else:
            outputs = self.topology.broadcast_ports(dp.id, ingress, eth.src)
        if not outputs:
            return
        actions = [parser.OFPActionOutput(port) for port in outputs]
        if known_unicast:
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, cookie=ROUTE_COOKIE, priority=10, idle_timeout=30,
                hard_timeout=120, match=parser.OFPMatch(
                    in_port=ingress, eth_src=eth.src, eth_dst=eth.dst),
                instructions=[parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)],
            ))
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=msg.buffer_id, in_port=ingress, actions=actions,
            data=msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None,
        ))


if __name__ == '__main__':
    import logging
    from os_ken import cfg

    logging.basicConfig(level=logging.INFO)
    cfg.CONF()
    app_manager.AppManager.run_apps([__file__])
