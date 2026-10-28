"""Nonblocking controller-side polling and an isolated SQLite/inference worker."""

from collections import defaultdict, deque
import logging
from pathlib import Path
import queue
import threading
import time

from telemetry import COUNTERS, PortSampler, aggregate_link, link_snapshot
from telemetry_store import TelemetryStore, utc_now


class PollCollector:
    """Called only from the controller's serialized event handlers."""
    def __init__(self, worker, scale=0.1, clock=time.monotonic, routing_epoch=lambda: None):
        self.worker, self.scale, self.clock = worker, scale, clock
        self.pending = {}
        self.frame = None
        self.fingerprint = None
        self.poll_id = 0
        self.rejected_replies = 0
        self.statistics_requests = deque(maxlen=256)
        self.routing_epoch = routing_epoch

    def topology_changed(self, topology, datapaths):
        links, ports = link_snapshot(topology, datapaths, self.scale)
        fingerprint = tuple((key, item['active'], item['token']) for key, item in sorted(ports.items()))
        if fingerprint != self.fingerprint:
            if self.frame:
                self._finish('topology_changed')
            self.fingerprint = fingerprint
            self.worker.submit('topology', dict(links=links, ports=ports,
                monotonic_time=self.clock(), observation_timestamp=utc_now(), routing_epoch=self.routing_epoch()))

    def poll(self, topology, datapaths):
        self.topology_changed(topology, datapaths)
        if self.frame:
            self._finish()
        self.poll_id += 1
        links, ports = link_snapshot(topology, datapaths, self.scale)
        now = self.clock()
        self.frame = dict(poll_id=self.poll_id, links=links, ports=ports, samples={}, failures={},
                          monotonic_time=now, observation_timestamp=utc_now(), routing_epoch=self.routing_epoch())
        self.pending = {}
        # Register all XIDs before requests can yield control to socket threads.
        requests = []
        for dpid, dp in datapaths.items():
            request = dp.ofproto_parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY)
            dp.set_xid(request)
            self.pending[dpid] = dict(dp=dp, xid=request.xid, parts={}, done=False)
            self.statistics_requests.append((id(dp), request.xid))
            requests.append((dp, request))
        for dp, request in requests:
            try:
                dp.send_msg(request)
            except Exception:
                self.pending[dp.id]['done'] = True
                self.frame['failures'][dp.id] = 'request_failed'
        if not self.pending or all(p['done'] for p in self.pending.values()):
            self._finish()

    def reply(self, msg):
        dp = msg.datapath
        pending = self.pending.get(dp.id)
        if (self.frame is None or pending is None or pending['dp'] is not dp or
                pending['xid'] != msg.xid or pending['done']):
            self.rejected_replies += 1
            return False
        if self.clock() - self.frame['monotonic_time'] > 4:
            self.rejected_replies += 1
            self.frame['failures'][dp.id] = 'late_reply'
            pending['done'] = True
            if all(p['done'] for p in self.pending.values()):
                self._finish()
            return False
        now, wall = self.clock(), utc_now()
        for stat in msg.body:
            key = (dp.id, stat.port_no)
            if stat.port_no >= dp.ofproto.OFPP_MAX or key not in self.frame['ports']:
                continue
            if key in pending['parts']:
                self.frame['failures'][dp.id] = 'duplicate_ports'
            pending['parts'][key] = dict(counters={name: getattr(stat, name, None) for name in COUNTERS},
                duration=getattr(stat, 'duration_sec', 0) + getattr(stat, 'duration_nsec', 0) / 1e9,
                monotonic_time=now, observation_timestamp=wall)
        if not msg.flags & dp.ofproto.OFPMPF_REPLY_MORE:
            pending['done'] = True
            self.frame['samples'].update(pending['parts'])
            if all(p['done'] for p in self.pending.values()):
                self._finish()
        return True

    def statistics_error(self, dp, xid):
        if (id(dp), xid) not in self.statistics_requests:
            return False
        pending = self.pending.get(dp.id)
        if pending and pending['dp'] is dp and pending['xid'] == xid:
            self.frame['failures'][dp.id] = 'statistics_error'
            pending['done'] = True
            if all(p['done'] for p in self.pending.values()):
                self._finish()
            return True
        return True

    def _finish(self, forced_status=None):
        frame = self.frame
        for dpid, pending in self.pending.items():
            if forced_status or not pending['done']:
                frame['failures'][dpid] = forced_status or 'missing'
        frame.update(monotonic_time=self.clock(), observation_timestamp=utc_now())
        self.worker.submit('sample', frame)
        self.pending, self.frame = {}, None


class TelemetryWorker:
    """Single writer and single inference thread; the controller only puts_nowait."""
    def __init__(self, path, source='live_mininet', model_path=None, logger=None, scale=0.1, clock=time.monotonic,
                 on_batch=None,experiment_config=None):
        self.path, self.source, self.model_path = Path(path), source, Path(model_path) if model_path else None
        self.logger = logger or logging.getLogger(__name__)
        self.scale = scale
        self.clock = clock
        self.on_batch = on_batch
        self.experiment_config = dict(experiment_config or {})
        self.queue = queue.Queue(maxsize=64)
        self.closed = False
        self.dropped_batches = 0
        self.failure = None
        self.thread = threading.Thread(target=self._run, name='flowrider-telemetry', daemon=True)
        self.thread.start()

    def submit(self, kind, payload):
        if self.closed or self.failure:
            return False
        try:
            self.queue.put_nowait((kind, payload))
            return True
        except queue.Full:
            self.dropped_batches += 1
            self.logger.error('Telemetry backlog full; dropped batch (%d total), forwarding continues',
                              self.dropped_batches)
            return False

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.thread.is_alive():
            try:
                self.queue.put(None, timeout=2)
            except queue.Full:
                self.logger.error('Telemetry shutdown backlog; committed SQLite batches remain safe')
            self.thread.join(timeout=5)

    def _run(self):
        from prediction import Predictor, load_model
        sampler, histories, previous_ports = PortSampler(), defaultdict(lambda: deque(maxlen=16)), {}
        predictor, model_stamp = Predictor(), None
        origin = None
        try:
            with TelemetryStore(self.path, self.source, dict(self.experiment_config,interval_seconds=5, horizon_seconds=15,
                                    bandwidth_scale=self.scale, utilization_policy='max_direction_tx',
                                    model_path=str(self.model_path) if self.model_path else None)) as store:
                self.logger.info('Telemetry session=%s source=%s database=%s',
                                 store.session_id, self.source, self.path)
                while True:
                    message = self.queue.get()
                    if message is None:
                        break
                    kind, frame = message
                    if kind == 'routing':
                        store.write_routing(frame['kind'], frame['record'])
                        continue
                    origin = frame['monotonic_time'] if origin is None else origin
                    if self.model_path:
                        try:
                            stamp = self.model_path.stat().st_mtime_ns if self.model_path.exists() else None
                            if stamp != model_stamp:
                                model_stamp = stamp
                                predictor = Predictor(load_model(self.model_path, self.source)) if stamp else Predictor()
                                self.logger.info('ML model status: %s', 'ready' if predictor.bundle else 'model_not_ready')
                        except Exception as error:
                            predictor = Predictor()
                            self.logger.error('ML model load failed; model_not_ready: %s', error)
                    if kind == 'topology':
                        changed = {key for key in frame['ports']
                                   if previous_ports.get(key) != frame['ports'][key]}
                        for key in changed:
                            sampler.previous.pop(key, None)
                        affected = {frame['ports'][key]['link_id'] for key in changed}
                        for link in affected:
                            histories.pop(link, None)
                        previous_ports = frame['ports']
                    else:
                        changed, affected = set(), set()
                    raw, directions, observations, predictions = [], [], [], []
                    measured = {}
                    if kind == 'sample':
                        for key, port in sorted(frame['ports'].items()):
                            sample = frame['samples'].get(key, {})
                            now = sample.get('monotonic_time', frame['monotonic_time'])
                            wall = sample.get('observation_timestamp', frame['observation_timestamp'])
                            failure = frame['failures'].get(key[0])
                            result = sampler.sample(key, sample.get('counters') if not failure else None,
                                now, port['capacity_mbps'], port['token'], port['active'], sample.get('duration'))
                            if failure and port['active']:
                                result['status'] = failure
                            row = dict(result, source_dpid=key[0], source_port=key[1], kind=port['kind'],
                                link_id=port['link_id'], capacity_mbps=port['capacity_mbps'],
                                observation_timestamp=wall, monotonic_elapsed_time=now - origin,
                                poll_id=frame['poll_id'], link_active=port['active'])
                            raw.append(row)
                            measured[key] = row
                    for link in frame['links']:
                        if kind == 'topology' and link['link_id'] not in affected:
                            continue
                        ends = [(link['source_dpid'], link['source_port']),
                                (link['destination_dpid'], link['destination_port'])]
                        directional = []
                        for index, key in enumerate(ends):
                            if key in measured:
                                other = ends[1 - index]
                                row = dict(measured[key], destination_dpid=other[0], destination_port=other[1])
                                directions.append(row)
                                directional.append(row)
                        aggregate = aggregate_link(directional, link['active'])
                        now = max((r['monotonic_time'] for r in directional), default=frame['monotonic_time'])
                        wall = max(directional, key=lambda r: r['monotonic_time'])['observation_timestamp'] if directional else frame['observation_timestamp']
                        observation = dict(link, **aggregate, observation_timestamp=wall,
                            monotonic_elapsed_time=now - origin, session_id=store.session_id,
                            source=self.source, schema_version=1, link_active=link['active'],
                            poll_id=frame.get('poll_id'))
                        if kind == 'topology' and link['active']:
                            observation['status'] = 'topology_changed'
                        observations.append(observation)
                        histories[link['link_id']].append(observation)
                        if self.clock() - now > 10:
                            prediction = Predictor().predict(list(histories[link['link_id']]))
                            prediction['status'] = 'stale_measurement'
                        else:
                            prediction = predictor.predict(list(histories[link['link_id']]))
                        predictions.append(prediction)
                        if prediction['status'] == 'valid':
                            self.logger.info('[FlowRider ML] Link: %s Current: %.1f%% Predicted (+15s): %.1f%% '
                                'Target: %s Congestion warning: %s', link['link_id'],
                                prediction['current_utilization'] * 100, prediction['predicted_utilization'] * 100,
                                prediction['target_timestamp'], 'YES' if prediction['congestion_predicted'] else 'NO')
                        else:
                            self.logger.info('[FlowRider ML] Link: %s status=%s', link['link_id'], prediction['status'])
                    store.write_batch(raw, directions, observations, predictions)
                    if self.on_batch and kind == 'sample':
                        try:
                            self.on_batch(dict(epoch=frame.get('routing_epoch'), source=self.source,
                                monotonic_time=frame['monotonic_time'], observations=observations,
                                predictions=predictions))
                        except Exception:
                            self.logger.exception('Routing notification failed; telemetry continues')
        except Exception as error:
            self.failure = str(error)
            self.logger.exception('Telemetry worker disabled; forwarding continues')
