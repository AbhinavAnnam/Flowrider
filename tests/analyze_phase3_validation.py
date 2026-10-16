"""Exploratory live-data diagnostics on training/validation ONLY; never promotes a model.

Alternative horizons and longer history are research diagnostics. The controller
continues using its original frozen ten-feature, +15-second artifact.
"""

import argparse
from bisect import bisect_left
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prediction import FEATURES, TOLERANCE, evaluate, feature_for, segments, timestamp
from telemetry_store import read_rows

EXTRA_FEATURES = ('lag_30s', 'lag_60s', 'mean_60s', 'std_60s', 'slope_60s')


def diagnostic_examples(records, horizon=15):
    examples = []
    for segment in segments(records):
        times = [r['monotonic_elapsed_time'] for r in segment]
        for index, row in enumerate(segment):
            features = feature_for(segment[max(0, index - 4):index + 1])
            if features is None:
                continue
            history = segment[:index + 1]
            now = times[index]
            lags = [min(history, key=lambda r: abs(r['monotonic_elapsed_time'] - (now - lag)))
                    for lag in (30, 60)]
            if any(abs(r['monotonic_elapsed_time'] - (now - lag)) > TOLERANCE
                   for r, lag in zip(lags, (30, 60))):
                continue
            values = [r['utilization'] for r in history if r['monotonic_elapsed_time'] >= now - 60]
            position = bisect_left(times, now + horizon)
            candidates = [j for j in (position - 1, position) if index < j < len(times)]
            if not candidates:
                continue
            target_index = min(candidates, key=lambda j: abs(times[j] - now - horizon))
            if abs(times[target_index] - now - horizon) > TOLERANCE:
                continue
            target = segment[target_index]
            extra = [r['utilization'] for r in lags] + [statistics.mean(values), statistics.pstdev(values),
                (row['utilization'] - lags[-1]['utilization']) / (now - lags[-1]['monotonic_elapsed_time'])]
            examples.append(dict(row, features=features, extended_features=features + extra,
                target=target['utilization'], label_elapsed_time=times[target_index],
                time=timestamp(row['observation_timestamp']).timestamp(),
                label_time=timestamp(target['observation_timestamp']).timestamp()))
    return examples


def analyze(training_records, validation_records, parameters):
    sources = {r['source'] for r in training_records + validation_records}
    if sources != {'live_mininet'}:
        raise ValueError('Diagnostics require exclusively live_mininet observations')
    if {r['session_id'] for r in training_records} & {r['session_id'] for r in validation_records}:
        raise ValueError('Training and validation must be different sessions')
    if max(timestamp(r['observation_timestamp']) for r in training_records) >= min(
            timestamp(r['observation_timestamp']) for r in validation_records):
        raise ValueError('Training must precede validation')
    from sklearn.ensemble import RandomForestRegressor
    horizons = {}
    for horizon in (5, 10, 15, 30):
        train, validation = diagnostic_examples(training_records, horizon), diagnostic_examples(validation_records, horizon)
        cutoff = min(r['time'] for r in validation) - 60 - TOLERANCE
        horizons[horizon] = ([r for r in train if r['label_time'] < cutoff], validation)
    # The same observation identities are used in all horizon/feature comparisons.
    key = lambda r: (r['session_id'], r['link_id'], r['time'])
    common_train = set.intersection(*({key(r) for r in train} for train, _ in horizons.values()))
    common_validation = set.intersection(*({key(r) for r in validation} for _, validation in horizons.values()))
    results, ablations = {}, {}
    for horizon, (train, validation) in horizons.items():
        train = [r for r in train if key(r) in common_train]
        validation = [r for r in validation if key(r) in common_validation]
        if len(train) < 60 or len(validation) < 20:
            raise ValueError('Insufficient diagnostic history')
        configurations = {'baseline_10_features': (list(range(10)), 'features')}
        if horizon == 15:
            configurations.update(current_only=([0], 'features'), four_lags=([0, 1, 2, 3], 'features'),
                                  extended_60s_history=(list(range(15)), 'extended_features'))
        baseline = evaluate([r['target'] for r in validation], [r['utilization'] for r in validation])
        for name, (indices, field) in configurations.items():
            model = RandomForestRegressor(random_state=42, n_jobs=1, **parameters)
            model.fit([[r[field][i] for i in indices] for r in train], [r['target'] for r in train])
            predicted = model.predict([[r[field][i] for i in indices] for r in validation]).tolist()
            result = dict(train_count=len(train), validation_count=len(validation),
                random_forest=evaluate([r['target'] for r in validation], predicted), persistence=baseline)
            if name == 'baseline_10_features':
                results[str(horizon)] = result
            if horizon == 15:
                ablations[name] = result
    return dict(source='live_mininet', scope='Exploratory training/validation only; final holdout and live inference excluded',
        parameters=parameters, horizons_seconds=results, feature_ablations_at_15s=ablations,
        base_features=list(FEATURES), additional_features=list(EXTRA_FEATURES),
        note='Longer-history comparisons use common observations and a 62-second boundary purge. No model is saved or promoted.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--training', required=True)
    parser.add_argument('--validation', required=True)
    parser.add_argument('--research-report', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    parameters = json.loads(Path(args.research_report).read_text())['selected_parameters']
    report = analyze(read_rows(args.training, 'observations'), read_rows(args.validation, 'observations'), parameters)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
