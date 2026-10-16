"""Causal timestamp-based features, evaluation, and inference. Never changes routes."""

from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta
import math
from pathlib import Path
import pickle
import statistics

HORIZON = 15
LOOKBACK = 15
TOLERANCE = 2
FEATURES = ('utilization', 'lag_5s', 'lag_10s', 'lag_15s', 'mean_15s',
            'slope_per_second', 'std_15s', 'tx_mbps', 'rx_mbps', 'capacity_mbps')


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('Timestamp must be timezone-aware')
    return parsed


def valid(row):
    required = ('utilization', 'tx_mbps', 'rx_mbps', 'capacity_mbps', 'monotonic_elapsed_time')
    return (row.get('valid') is True and row.get('link_active') is True and
            all(isinstance(row.get(k), (int, float)) and math.isfinite(row[k]) for k in required) and
            row['capacity_mbps'] > 0 and row['utilization'] >= 0)


def feature_for(history):
    if len(history) < 4 or not valid(history[-1]):
        return None
    current = history[-1]
    now = current['monotonic_elapsed_time']
    selected = []
    for lag in (0, 5, 10, 15):
        candidates = [r for r in history if r['monotonic_elapsed_time'] <= now]
        nearest = min(candidates, key=lambda r: abs(r['monotonic_elapsed_time'] - (now - lag)))
        if not valid(nearest) or abs(nearest['monotonic_elapsed_time'] - (now - lag)) > TOLERANCE:
            return None
        selected.append(nearest)
    earliest = selected[-1]['monotonic_elapsed_time']
    window = [r for r in history if earliest <= r['monotonic_elapsed_time'] <= now]
    if any(not valid(r) for r in window) or any(
            not 0 < b['monotonic_elapsed_time'] - a['monotonic_elapsed_time'] <= 7
            for a, b in zip(window, window[1:])):
        return None
    if len({r['monotonic_elapsed_time'] for r in selected}) != 4:
        return None
    values = [r['utilization'] for r in selected]
    return values + [statistics.mean(values), (values[0] - values[-1]) / (now - earliest),
                     statistics.pstdev(values), current['tx_mbps'], current['rx_mbps'],
                     current['capacity_mbps']]


def segments(rows):
    groups = defaultdict(list)
    for row in rows:
        if row.get('schema_version') != 1:
            raise ValueError('Unsupported observation schema')
        groups[row['session_id'], row['link_id']].append(row)
    for group in groups.values():
        previous, segment = None, []
        for row in group:
            time = row['monotonic_elapsed_time']
            wall = timestamp(row['observation_timestamp']).timestamp()
            if previous and time <= previous['monotonic_elapsed_time']:
                raise ValueError('Observations must be strictly ordered within each session/link')
            discontinuity = previous and (
                time - previous['monotonic_elapsed_time'] > 7 or
                abs((wall - timestamp(previous['observation_timestamp']).timestamp()) -
                    (time - previous['monotonic_elapsed_time'])) > TOLERANCE)
            if discontinuity or not valid(row):
                if segment:
                    yield segment
                segment = []
            if valid(row):
                segment.append(row)
            previous = row
        if segment:
            yield segment


def build_examples(rows):
    examples = []
    for segment in segments(rows):
        times = [r['monotonic_elapsed_time'] for r in segment]
        for index, row in enumerate(segment):
            features = feature_for(segment[max(0, index - 4):index + 1])
            if features is None:
                continue
            desired = times[index] + HORIZON
            position = bisect_left(times, desired)
            candidates = [i for i in (position - 1, position) if index < i < len(segment)]
            if not candidates:
                continue
            label_index = min(candidates, key=lambda i: abs(times[i] - desired))
            if abs(times[label_index] - desired) > TOLERANCE:
                continue
            target = segment[label_index]
            examples.append(dict(row, features=features, target=target['utilization'],
                label_elapsed_time=times[label_index],
                time=timestamp(row['observation_timestamp']).timestamp(),
                label_time=timestamp(target['observation_timestamp']).timestamp(),
                label_timestamp=target['observation_timestamp']))
    return sorted(examples, key=lambda r: (r['time'], r['session_id'], r['link_id']))


def chronological_split(examples, test_fraction=0.2, holdout_session=None):
    if not 0 < test_fraction < 1 or not examples:
        raise ValueError('No labeled examples or invalid test fraction')
    times = sorted({r['time'] for r in examples})
    if holdout_session:
        test = [r for r in examples if r['session_id'] == holdout_session]
        if not test:
            raise ValueError('Holdout session has no labeled examples')
        cutoff = min(r['time'] for r in test)
    else:
        cutoff = times[min(len(times) - 1, int(len(times) * (1 - test_fraction)))]
        test = [r for r in examples if r['time'] >= cutoff]
    # Latest training label must precede the earliest test history window.
    train = [r for r in examples if r['label_time'] < cutoff - LOOKBACK - TOLERANCE
             and (not holdout_session or r['session_id'] != holdout_session)]
    return train, test


def evaluate(actual, predicted, threshold=0.8):
    if len(actual) != len(predicted) or not actual:
        raise ValueError('Evaluation requires paired nonempty arrays')
    if any(not math.isfinite(float(v)) for v in list(actual) + list(predicted)):
        raise ValueError('Non-finite evaluation value')
    count = len(actual)
    errors = [p - a for a, p in zip(actual, predicted)]
    denominator = sum((a - statistics.mean(actual)) ** 2 for a in actual)
    result = dict(sample_count=count, mae=sum(abs(e) for e in errors) / count,
                  rmse=math.sqrt(sum(e * e for e in errors) / count),
                  r2=1 - sum(e * e for e in errors) / denominator if count > 1 and denominator else None)
    positives = sum(a >= threshold for a in actual)
    true_positive = sum(a >= threshold and p >= threshold for a, p in zip(actual, predicted))
    false_positive = sum(a < threshold and p >= threshold for a, p in zip(actual, predicted))
    result.update(positive_event_count=positives, negative_event_count=count - positives,
                  predicted_positive_count=sum(p >= threshold for p in predicted))
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / positives if positives else 0.0
    result.update(congestion_precision=precision if positives else None,
                  congestion_recall=recall if positives else None,
                  congestion_f1=(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
                  if positives else None,
                  false_alarm_rate=false_positive / (count - positives) if positives and count > positives else None,
                  congestion_metrics_status='available' if positives else 'unavailable_no_positive_events')
    return result


def load_model(path, source='live_mininet'):
    # Pickle is only for locally trained, trusted artifacts; never accept uploads.
    import sklearn
    with Path(path).open('rb') as handle:
        bundle = pickle.load(handle)
    metadata = bundle['metadata']
    if (metadata['source'] != source or metadata['features'] != list(FEATURES) or
            metadata['horizon_seconds'] != HORIZON or metadata['schema_version'] != 1 or
            metadata['sklearn_version'] != sklearn.__version__):
        raise ValueError('Model provenance/schema/version mismatch')
    return bundle


class Predictor:
    def __init__(self, bundle=None):
        self.bundle = bundle

    def predict(self, history):
        if not history:
            return dict(status='empty_history', predicted_utilization=None,
                        congestion_predicted=None, prediction_horizon_seconds=HORIZON)
        row = history[-1]
        metadata = self.bundle['metadata'] if self.bundle else {}
        result = dict(link_id=row['link_id'], session_id=row['session_id'], source=row['source'],
            observation_timestamp=row['observation_timestamp'],
            target_timestamp=(timestamp(row['observation_timestamp']) + timedelta(seconds=HORIZON)).isoformat(),
            target_monotonic_elapsed_time=row['monotonic_elapsed_time'] + HORIZON,
            prediction_horizon_seconds=HORIZON, current_utilization=row.get('utilization'),
            predicted_utilization=None, raw_predicted_utilization=None,
            congestion_threshold=metadata.get('threshold', 0.8), congestion_predicted=None,
            model_version=metadata.get('model_version'), status='model_not_ready')
        if not valid(row):
            result['status'] = row.get('status', 'invalid_measurement')
            return result
        if self.bundle is None:
            return result
        if metadata.get('source', row['source']) != row['source']:
            result['status'] = 'model_provenance_mismatch'
            return result
        features = feature_for(history)
        if features is None:
            result['status'] = 'insufficient_history'
            return result
        try:
            raw = float(self.bundle['model'].predict([features])[0])
            if not math.isfinite(raw):
                raise ValueError('Non-finite model output')
            clipped = min(1.0, max(0.0, raw))
            result.update(raw_predicted_utilization=raw, predicted_utilization=clipped,
                          congestion_predicted=clipped >= result['congestion_threshold'], status='valid')
        except Exception as error:
            result.update(status='inference_error', error=str(error))
        return result
