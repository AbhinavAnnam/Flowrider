"""Read-only demo view over Phase 3 data and existing topology/Dijkstra code."""

from collections import defaultdict, deque
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time

from network_topology import calculate_shortest_path
from prediction import timestamp
from telemetry_store import utc_now
from topology_config import SWITCHES
from topology_state import TopologyState


def epoch(row):
    return timestamp(row['observation_timestamp']).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def read_session(path, limited=False):
    """Never creates a database or writes to the controller's SQLite connection."""
    with sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True, timeout=.5) as connection:
        connection.row_factory = sqlite3.Row
        session = connection.execute('SELECT * FROM sessions ORDER BY started_timestamp DESC LIMIT 1').fetchone()
        if not session or session['source'] != 'live_mininet':
            raise ValueError('Demo requires recorded or live Mininet measurements, not synthetic data')
        session = dict(session)
        rows = {}
        for table in ('observations', 'predictions'):
            query = f'SELECT record_json FROM {table} WHERE session_id=? ORDER BY id DESC'
            if limited:
                query += ' LIMIT 1400'
            rows[table] = sorted([json.loads(r[0]) for r in connection.execute(query, (session['session_id'],))], key=epoch)
    return session, rows['observations'], rows['predictions']


def calculate_routes(links, source, destination):
    graph = TopologyState().graph
    weights = []
    missing = []
    for link in links:
        u, v = link['source'], link['target']
        if link['active'] is not True:
            graph.remove_edge(u, v)
            continue
        edge = graph[u][v]
        predicted = link['predicted_utilization']
        edge['capacity'] = link['capacity_mbps']
        edge['current_load'] = (link['current_utilization'] or 0) * link['capacity_mbps']
        if predicted is None:
            missing.append(link['id'])
        else:
            edge['ml_weight'] = edge['base_delay'] * (1 + 25 * min(1, max(0, predicted)) ** 4)
            weights.append(dict(link_id=link['id'], weight=edge['ml_weight'],
                                base_delay=edge['base_delay'], predicted_utilization=predicted))
    def route(weight):
        result = calculate_shortest_path(graph, source, destination, weight_metric=weight)
        if result is None:
            return None
        path = result['path']
        return dict(path=[SWITCHES[n] for n in path],
            links=[f's{min(u,v)}-s{max(u,v)}' for u,v in zip(path,path[1:])],
            cost=result['total_latency_ms'],
            base_delay_ms=sum(graph[u][v]['base_delay'] for u,v in zip(path,path[1:])),
            hops=result['hop_count'])
    baseline = route('base_delay')
    proposed = route('ml_weight') if not missing else None
    changed = bool(baseline and proposed and baseline['path'] != proposed['path'])
    explanation = ('Predicted load increases link costs; Dijkstra selects a different calculated path.' if changed else
        'The prediction-weighted calculation currently selects the baseline path.' if proposed else
        'A preview needs a connected path and recent valid predictions on every active link.')
    return dict(source=source, destination=destination, baseline=baseline, proposed=proposed,
        changed=changed, weights=weights, forwarding_verified=False,
        forwarding_status='Packet path is not flow-verified. Phase 2 uses the base-delay policy.',
        status='preview_only' if proposed else 'preview_unavailable', explanation=explanation,
        missing_prediction_links=missing, equation='W = D₀ × (1 + 25 × Û⁴)')


class DemoData:
    def __init__(self, replay_db, live_db, mode='replay', clock=time.monotonic):
        self.replay_db, self.live_db = Path(replay_db), Path(live_db)
        self.clock = clock
        self.lock = threading.RLock()
        self.mode, self.playing, self.speed = mode, False, 4
        self.position, self.anchor = 0, self.clock()
        self.source, self.destination = 1, 4
        self.ui_events = deque(maxlen=8)
        self.recording_error = None
        self.session, self.observations, self.predictions = {}, [], []
        try:
            self.session, self.observations, self.predictions = read_session(self.replay_db)
            first_by_link = {}
            for row in self.predictions:
                if row['status'] == 'valid':
                    first_by_link.setdefault(row['link_id'], epoch(row))
            if len(first_by_link) != 7:
                raise ValueError('Recording needs valid saved forecasts for all seven configured links')
            self.start = max(first_by_link.values()) + .05
            self.end = max(epoch(r) for r in self.observations if r.get('valid'))
            self.event_times, self.recorded_events = self._recorded_events()
        except (OSError, ValueError, sqlite3.Error, KeyError) as error:
            self.recording_error = str(error)
            self.start, self.end, self.event_times, self.recorded_events = 0, 1, [], []

    def _recorded_events(self):
        events, times, forecast_times, previous = [], [], [], {}
        for row in self.observations:
            old = previous.get(row['link_id'])
            if row.get('valid') and row['utilization'] >= .8 and not (old and old.get('valid') and old['utilization'] >= .8):
                events.append(dict(timestamp=row['observation_timestamp'], kind='threshold',
                    message=f"{row['link_id']} measured above 80%", origin='recorded telemetry'))
                times.append(epoch(row))
            if old and old['link_active'] != row['link_active']:
                events.append(dict(timestamp=row['observation_timestamp'], kind='topology',
                    message=f"{row['link_id']} {'active' if row['link_active'] else 'inactive'}", origin='recorded topology'))
            previous[row['link_id']] = row
        previous = {}
        for row in self.predictions:
            old = previous.get(row['link_id'])
            if row['status'] == 'valid' and row['congestion_predicted'] and not (old and old.get('congestion_predicted')):
                events.append(dict(timestamp=row['observation_timestamp'], kind='prediction',
                    message=f"{row['link_id']} +15s forecast above 80%", origin='recorded forecast'))
                forecast_times.append(epoch(row))
            previous[row['link_id']] = row
        grouped = []
        for value in sorted(t for t in (forecast_times or times) if self.start < t < self.end):
            if not grouped or value - grouped[-1] > 3:
                grouped.append(value + .1)
        return grouped, sorted(events, key=lambda r: timestamp(r['timestamp']))

    def _position(self):
        position = self.position + ((self.clock() - self.anchor) * self.speed if self.playing else 0)
        return min(max(0, position), self.end - self.start)

    def control(self, body):
        with self.lock:
            action = body.get('action')
            position = self._position()
            message = None
            if action in ('start', 'pause'):
                self.position, self.anchor = position, self.clock()
                if action == 'start' and position >= self.end - self.start:
                    self.position = 0
                self.playing = action == 'start'
                message = 'Recorded playback started' if self.mode == 'replay' else 'Live view refreshed; no network process started'
            elif action == 'reset':
                self.position, self.anchor, self.playing = 0, self.clock(), False
                self.source, self.destination = 1, 4
                self.ui_events.clear()
                message = 'Display reset; recorded and live datasets are unchanged'
            elif action == 'next_event':
                if self.mode != 'replay':
                    raise ValueError('Next Event is available in recorded replay mode')
                now = self.start + position
                target = next((t for t in self.event_times if t > now + 2), self.event_times[0] if self.event_times else self.start)
                self.position, self.anchor, self.playing = target - self.start, self.clock(), False
                message = 'Jumped to an actual recorded forecast-warning event'
            elif action == 'mode':
                if body.get('mode') not in ('live', 'replay'):
                    raise ValueError('Choose live or replay')
                if body['mode'] == 'replay' and self.recording_error:
                    raise ValueError(self.recording_error)
                self.mode, self.playing, self.anchor = body['mode'], False, self.clock()
                message = f"Switched to {self.mode.upper()} view"
            elif action == 'seek':
                value = float(body.get('position', -1))
                if not 0 <= value <= self.end - self.start:
                    raise ValueError('Playback position is outside the recording')
                self.position, self.anchor = value, self.clock()
            elif action == 'speed':
                speed = float(body.get('speed', 0))
                if speed not in (1, 2, 4):
                    raise ValueError('Supported playback speeds: 1, 2, 4')
                self.position, self.anchor, self.speed = position, self.clock(), speed
            elif action == 'route':
                source, destination = int(body.get('source', 0)), int(body.get('destination', 0))
                if source not in SWITCHES or destination not in SWITCHES or source == destination:
                    raise ValueError('Choose two different configured switches')
                self.source, self.destination = source, destination
                message = f'Recalculated s{source} → s{destination} preview; no forwarding rules changed'
            else:
                raise ValueError('Unsupported demo control')
            if message:
                self.ui_events.appendleft(dict(timestamp=utc_now(), kind='control', origin='dashboard only', message=message))
            return self.snapshot()

    def snapshot(self):
        with self.lock:
            position = self._position()
            if position >= self.end - self.start:
                self.position, self.playing = position, False
            recorded = self.mode == 'replay'
            as_of = self.start + position if recorded else time.time()
            error = self.recording_error if recorded else None
            if recorded:
                session, observations, predictions = self.session, self.observations, self.predictions
            else:
                try:
                    session, observations, predictions = read_session(self.live_db, limited=True)
                except (OSError, sqlite3.Error, ValueError, KeyError) as exception:
                    session, observations, predictions, error = {}, [], [], str(exception)
            observations = [r for r in observations if epoch(r) <= as_of]
            predictions = [r for r in predictions if epoch(r) <= as_of]
            latest, forecasts = {}, {}
            for row in observations:
                latest[row['link_id']] = row
            for row in predictions:
                forecasts[row['link_id']] = row
            scale = json.loads(session.get('config_json', '{}')).get('bandwidth_scale', .1)
            links, histories = [], defaultdict(list)
            prediction_by_time = {(p['link_id'], p['observation_timestamp']): p for p in predictions}
            for row in observations:
                if row.get('valid') and epoch(row) >= as_of - 120:
                    pred = prediction_by_time.get((row['link_id'], row['observation_timestamp']), {})
                    histories[row['link_id']].append(dict(timestamp=row['observation_timestamp'],
                        current=row['utilization'], predicted=pred.get('predicted_utilization') if pred.get('status') == 'valid' else None,
                        target_timestamp=pred.get('target_timestamp')))
            for u, v, edge in TopologyState().graph.edges(data=True):
                link_id = f's{min(u,v)}-s{max(u,v)}'
                row, pred = latest.get(link_id), forecasts.get(link_id)
                age = as_of - epoch(row) if row else None
                recent = row is not None and 0 <= age <= 10
                valid = recent and row.get('valid') and row.get('link_active')
                prediction_valid = bool(valid and pred and pred['status'] == 'valid' and
                    0 <= as_of - epoch(pred) <= 10 and abs(epoch(pred) - epoch(row)) <= 2 and
                    timestamp(pred['target_timestamp']).timestamp() > as_of)
                utilization = row.get('utilization') if valid else None
                forecast = pred.get('predicted_utilization') if prediction_valid else None
                links.append(dict(id=link_id, source=u, target=v, source_port=edge['ports'][u],
                    target_port=edge['ports'][v], capacity_mbps=row.get('capacity_mbps') if row else edge['capacity'] * scale,
                    base_delay_ms=edge['base_delay'], active=row.get('link_active') if recent else None,
                    current_utilization=utilization, predicted_utilization=forecast,
                    congested=utilization is not None and utilization >= .8,
                    congestion_predicted=forecast is not None and forecast >= .8,
                    observation_timestamp=row.get('observation_timestamp') if row else None,
                    target_timestamp=pred.get('target_timestamp') if prediction_valid else None,
                    model_version=pred.get('model_version') if prediction_valid else None,
                    telemetry_status=row.get('status') if recent else 'stale' if row else 'unavailable',
                    prediction_status=pred.get('status') if prediction_valid else 'not_ready', age_seconds=age))
            recent_count = sum(l['current_utilization'] is not None for l in links)
            prediction_count = sum(l['predicted_utilization'] is not None for l in links)
            state = 'recorded' if recorded and not self.recording_error else 'fresh' if not recorded and recent_count else 'stale' if observations else 'unavailable'
            events = [e for e in self.recorded_events if timestamp(e['timestamp']).timestamp() <= as_of][-8:][::-1] if recorded else []
            if not recorded:
                for link in links:
                    if link['congested'] or link['congestion_predicted']:
                        events.append(dict(timestamp=link['observation_timestamp'], kind='prediction',
                            message=f"{link['id']} {'forecast' if link['congestion_predicted'] else 'measured'} above 80%",
                            origin='live telemetry'))
            return dict(mode=self.mode, recorded=recorded, observation_clock=iso(as_of), generated_at=utc_now(),
                playback=dict(playing=self.playing, speed=self.speed, position=position,
                    duration=self.end-self.start, start_timestamp=iso(self.start), end_timestamp=iso(self.end)),
                health=dict(backend='ready', controller='recorded' if recorded else 'telemetry_observed' if recent_count else 'not_observed',
                    telemetry=state, model='recorded_forecasts' if recorded and prediction_count else 'ready' if prediction_count else 'not_ready',
                    recent_links=recent_count, predicted_links=prediction_count, error=error,
                    model_version=next((l['model_version'] for l in links if l['model_version']), None)),
                source=dict(session_id=session.get('session_id'), provenance=session.get('source'),
                    database=(self.replay_db if recorded else self.live_db).name),
                topology=dict(nodes=[dict(id=i, name=name, host=f'h{i}', observed=any(l['active'] and i in (l['source'],l['target']) for l in links))
                    for i,name in SWITCHES.items()], links=links), history=dict(histories),
                routes=calculate_routes(links, self.source, self.destination),
                events=list(self.ui_events)[:3] + events, congestion_threshold=.8,
                notice='RECORDED MININET DATA · Original timestamps · Route previews only' if recorded else
                    'LIVE read-only telemetry · Route previews only · No OpenFlow updates from this dashboard')
