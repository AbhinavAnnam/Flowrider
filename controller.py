"""OS-Ken/OpenFlow 1.3 baseline and opt-in predictive forwarding for FlowRider."""

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls,
)
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet, ethernet, ether_types
from os_ken.lib.packet import vlan
from os_ken import cfg
from os_ken.controller import event
from os_ken.lib import hub
import threading
import json
from pathlib import Path
import uuid

from topology_state import TopologyState


ROUTE_COOKIE = 0x4652000000000000
COOKIE_MASK = 0xFFFF000000000000

# The direct launcher is imported again by AppManager after argument parsing.
if 'telemetry_enabled' not in cfg.CONF:
    cfg.CONF.register_cli_opts([
        cfg.BoolOpt('telemetry-enabled', default=False),
        cfg.StrOpt('telemetry-db', default='data/telemetry.sqlite'),
        cfg.StrOpt('telemetry-model', default=None),
        cfg.StrOpt('telemetry-source', default='live_mininet', choices=['live_mininet', 'simulated_openflow']),
        cfg.FloatOpt('telemetry-bandwidth-scale', default=0.1),
    ])

if 'predictive_routing' not in cfg.CONF:
    cfg.CONF.register_cli_opts([
        cfg.BoolOpt('predictive-routing', default=False),
        cfg.FloatOpt('routing-min-improvement', default=.10),
        cfg.FloatOpt('routing-cooldown', default=30),
        cfg.IntOpt('routing-confirmations', default=2),
        cfg.FloatOpt('routing-prediction-max-age', default=10),
        cfg.FloatOpt('routing-operation-timeout', default=3),
        cfg.FloatOpt('routing-drain-seconds', default=1),
    ])

if 'routing_mode' not in cfg.CONF:
    cfg.CONF.register_cli_opts([
        cfg.StrOpt('routing-mode',default=None,choices=['baseline','predictive_ml']),
        cfg.StrOpt('topology',default='six',choices=['six','phase6c']),
        cfg.StrOpt('trial-id',default=None),
        cfg.StrOpt('traffic-config',default=None,help='JSON trial configuration to record with telemetry'),
        cfg.StrOpt('experiment-link-capacities',default=None,
                   help='Opt-in JSON mapping of link IDs to applied physical Mbps for experiments'),
    ])


def resolve_routing_mode(predictive_flag,requested):
    if requested not in (None,'baseline','predictive_ml'):raise ValueError('Unknown routing mode')
    if predictive_flag and requested=='baseline':
        raise ValueError('--predictive-routing conflicts with --routing-mode baseline')
    return requested or ('predictive_ml' if predictive_flag else None)


class EventTelemetryPoll(event.EventBase):
    pass


class EventRoutingTick(event.EventBase):
    pass


class EventRoutingTelemetry(event.EventBase):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame


class FlowRiderController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(FlowRiderController, self).__init__(*args, **kwargs)
        self.topology = TopologyState(self.CONF.topology)
        self.datapaths = {}
        self.mac_to_port = {}
        self.pending_barriers = {}
        self.validated = set()
        self.failed = set()
        self.port_snapshots = {}
        self.port_overrides = {}
        self.telemetry = None
        self.telemetry_worker = None
        self.telemetry_stop = threading.Event()
        self.routing = None

    def start(self):
        mode=resolve_routing_mode(self.CONF.predictive_routing,self.CONF.routing_mode)
        trial_id=self.CONF.trial_id or uuid.uuid4().hex
        traffic=json.loads(Path(self.CONF.traffic_config).read_text()) if self.CONF.traffic_config else None
        if traffic is not None and not isinstance(traffic,dict):raise ValueError('Traffic configuration must be a JSON object')
        capacities=json.loads(self.CONF.experiment_link_capacities) if self.CONF.experiment_link_capacities else {}
        if capacities:
            if not mode or not self.CONF.telemetry_enabled:
                raise ValueError('Experiment capacities require an explicit telemetry-enabled routing mode')
            from phase6c import apply_capacities
            apply_capacities(self.topology,capacities,self.CONF.telemetry_bandwidth_scale)
        experiment=dict(routing_mode=mode or 'legacy',trial_id=trial_id,
                        topology_version=self.topology.topology_version,traffic_configuration=traffic,
                        experiment_link_capacities_mbps=capacities)
        if mode:
            if not self.CONF.telemetry_enabled:
                raise ValueError('Explicit routing modes and --predictive-routing require --telemetry-enabled')
            from predictive_routing import RoutingManager, RoutingPolicy
            self.routing = RoutingManager(self.topology, self.datapaths,
                policy=RoutingPolicy(min_improvement=self.CONF.routing_min_improvement,
                    cooldown=self.CONF.routing_cooldown, confirmations=self.CONF.routing_confirmations,
                    prediction_max_age=self.CONF.routing_prediction_max_age,
                    operation_timeout=self.CONF.routing_operation_timeout,
                    drain_seconds=self.CONF.routing_drain_seconds),
                record=self._record_routing, release=self._release_routing_packet,
                source=self.CONF.telemetry_source, scale=self.CONF.telemetry_bandwidth_scale,
                routing_mode=mode,trial_id=trial_id)
        super().start()
        if self.CONF.telemetry_enabled:
            try:
                from telemetry_runtime import PollCollector, TelemetryWorker
                self.telemetry_worker = TelemetryWorker(
                    self.CONF.telemetry_db, source=self.CONF.telemetry_source,
                    model_path=self.CONF.telemetry_model, logger=self.logger,
                    scale=self.CONF.telemetry_bandwidth_scale,
                    on_batch=self._routing_batch if self.routing else None,experiment_config=experiment)
                self.telemetry = PollCollector(self.telemetry_worker,
                    scale=self.CONF.telemetry_bandwidth_scale,
                    routing_epoch=lambda: self.routing.epoch if self.routing else None)
                self.threads.append(hub.spawn(self._telemetry_clock))
            except Exception:
                self.logger.exception('Telemetry startup failed; forwarding continues')
        if self.routing:
            self.threads.append(hub.spawn(self._routing_clock))

    def _routing_clock(self):
        while not self.telemetry_stop.is_set():
            self.send_event(self.name, EventRoutingTick())
            self.telemetry_stop.wait(1)

    def _record_routing(self, kind, row):
        if kind == 'event':
            self.logger.info('[FlowRider routing] %s route=%s %s', row['kind'], row.get('route_id'), row)
        if self.telemetry_worker and not self.telemetry_worker.submit('routing', dict(kind=kind, record=row)):
            self.logger.warning('Routing history could not be queued; controller state remains authoritative')

    def _routing_batch(self, frame):
        self.send_event(self.name, EventRoutingTelemetry(frame))

    @set_ev_cls(EventRoutingTelemetry)
    def routing_telemetry_handler(self, ev):
        if self.routing and not self.pending_barriers:
            self.routing.telemetry(ev.frame)

    @set_ev_cls(EventRoutingTick)
    def routing_tick_handler(self, ev):
        if self.routing and not self.pending_barriers:
            self.routing.tick()

    def _release_routing_packet(self, queued):
        dp = self.datapaths.get(queued['dpid'])
        if dp is not queued['datapath'] or dp.id not in self.validated or self.pending_barriers:
            return
        p, o = dp.ofproto_parser, dp.ofproto
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=o.OFP_NO_BUFFER,
            in_port=queued['ingress'], actions=[p.OFPActionOutput(o.OFPP_TABLE)], data=queued['data']))

    def _telemetry_clock(self):
        while not self.telemetry_stop.is_set():
            self.send_event(self.name, EventTelemetryPoll())
            # OS-Ken 4.2.2 uses native threads; Event.wait also permits prompt stop.
            self.telemetry_stop.wait(5)

    def stop(self):
        self.telemetry_stop.set()
        if self.telemetry_worker:
            self.telemetry_worker.close()
        super().stop()

    def _telemetry_call(self, method, *args):
        if self.telemetry is not None:
            try:
                return getattr(self.telemetry, method)(*args)
            except Exception:
                self.logger.exception('Telemetry operation failed; forwarding continues')

    @set_ev_cls(EventTelemetryPoll)
    def telemetry_poll_handler(self, ev):
        # In OS-Ken 4.2.2's native hub, serve() can wait for helper threads
        # after _recv_loop has closed the socket, delaying its DEAD event.
        for dp in list(self.datapaths.values()):
            if getattr(dp, 'socket', None) is not None and dp.socket.fileno() < 0:
                dp.close()  # Emits the normal state-change event; never closes a live socket.
        self._telemetry_call('poll', self.topology,
                             {i: dp for i, dp in self.datapaths.items()
                              if i not in self.failed and getattr(dp, 'state', None) != DEAD_DISPATCHER})

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def port_stats_reply_handler(self, ev):
        if self._current(ev.msg.datapath):
            self._telemetry_call('reply', ev.msg)

    def _current(self, datapath):
        return self.datapaths.get(datapath.id) is datapath

    def _invalidate_routes(self):
        """Pause PacketIn forwarding until every healthy switch confirms deletes.

        The table-miss remains in place. Cookies restrict deletion to our routes.
        Barrier replies from previous connections/updates cannot release the pause.
        """
        if self.routing:
            self.routing.topology_changed()
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
        self._telemetry_call('topology_changed', self.topology, self.datapaths)

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
        if self.routing and self.routing.barrier_reply(msg):
            return
        if self._current(dp) and self.pending_barriers.get(key) is dp:
            del self.pending_barriers[key]
            if not self.pending_barriers:
                self.logger.info('Forwarding ready: switches=%s active_links=%d',
                                 sorted(self.validated), self.topology.active_graph().number_of_edges())

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def routing_stats_reply_handler(self, ev):
        if self.routing and self._current(ev.msg.datapath):
            self.routing.stats_reply(ev.msg)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def routing_flow_removed_handler(self, ev):
        if self.routing and self._current(ev.msg.datapath):
            self.routing.flow_removed(ev.msg)

    @set_ev_cls(ofp_event.EventOFPErrorMsg, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def error_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        if not self._current(dp):
            return
        if self.routing and self.routing.error(msg):
            self.logger.warning('Predictive route operation failed: DPID=%d type=%d code=%d', dp.id, msg.type, msg.code)
            return
        if self._telemetry_call('statistics_error', dp, getattr(msg, 'xid', None)):
            self.logger.warning('OpenFlow telemetry request error: DPID=%d type=%d code=%d',
                                dp.id, msg.type, msg.code)
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
        decoded = packet.Packet(msg.data)
        eth = decoded.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return
        if int(eth.src.split(':')[0], 16) & 1:
            return
        if self.routing and (dp.id, ingress) in self.topology.access_ports and decoded.get_protocol(vlan.vlan):
            return  # Internal version tags are never accepted from a lab host.
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
            if self.routing:
                tag = decoded.get_protocol(vlan.vlan)
                if tag:
                    self.routing.transit_miss(eth.src, eth.dst, tag.vid)
                    return
                if (dp.id, ingress) != self.topology.hosts.get(eth.src):
                    return
                if len(msg.data) < getattr(msg, 'total_len', len(msg.data)):
                    return
                self.routing.ensure(eth.src, eth.dst, packet=dict(dpid=dp.id, datapath=dp,
                    ingress=ingress, data=msg.data))
                return
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
