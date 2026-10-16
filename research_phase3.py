"""Offline independent-session validation and one final holdout evaluation.

This tool never generates observations, polls switches, or changes forwarding.
Only run against a frozen dataset; choose a new holdout after changing the protocol.
"""

import argparse
from collections import Counter
from datetime import timedelta
import json
import math
from pathlib import Path
import tempfile

from prediction import (FEATURES, LOOKBACK, TOLERANCE, build_examples,
                        evaluate, load_model, timestamp)
from telemetry_store import read_rows
from traffic_engine import train_live_predictor


CANDIDATES = (
    dict(n_estimators=100, min_samples_leaf=2),
    dict(n_estimators=100, min_samples_leaf=1, max_depth=12),
    dict(n_estimators=100, min_samples_leaf=5),
    dict(n_estimators=100, min_samples_leaf=2, max_features=0.6),
)


def research_split(examples):
    groups = {}
    for row in examples:
        groups.setdefault(row['session_id'], []).append(row)
    sessions = sorted(groups, key=lambda key: min(r['time'] for r in groups[key]))
    if len(sessions) < 3:
        raise ValueError('Need at least three independent sessions: training, validation, holdout')
    for first, second in zip(sessions, sessions[1:]):
        if max(r['label_time'] for r in groups[first]) >= min(r['time'] for r in groups[second]):
            raise ValueError('Independent sessions overlap in UTC time')
    validation, test = groups[sessions[-2]], groups[sessions[-1]]
    train_cutoff = min(r['time'] for r in validation) - LOOKBACK - TOLERANCE
    validation_cutoff = min(r['time'] for r in test) - LOOKBACK - TOLERANCE
    train = [r for key in sessions[:-2] for r in groups[key] if r['label_time'] < train_cutoff]
    validation = [r for r in validation if r['label_time'] < validation_cutoff]
    if not train or not validation:
        raise ValueError('No examples after chronological boundary purge')
    return train, validation, test


def comparison(actual, predicted, current):
    forest, persistence = evaluate(actual, predicted), evaluate(actual, current)
    # Positive error reduction means improvement; negative means regression.
    improvement = {name: (100 * (persistence[name] - forest[name]) / persistence[name]
                          if persistence[name] else None) for name in ('mae', 'rmse')}
    return dict(random_forest=forest, persistence=persistence,
                error_reduction_percent=improvement)


def audit_predictions(observations, predictions, metadata):
    """Check a frozen model's unseen-session predictions against later counters."""
    known_sessions = set(metadata.get('train_sessions', []) + metadata.get('test_sessions', []))
    if any(row['session_id'] in known_sessions for row in observations):
        raise ValueError('Inference audit requires an unseen session')
    labels = {(r['session_id'], r['link_id'], r['observation_timestamp']): r
              for r in build_examples(observations)}
    rows_by_key = {(r['session_id'], r['link_id'], r['observation_timestamp']): r for r in observations}
    pairs, checked = [], 0
    for prediction in predictions:
        if prediction['status'] != 'valid':
            continue
        key = (prediction['session_id'], prediction['link_id'], prediction['observation_timestamp'])
        row = rows_by_key.get(key)
        if (not row or prediction['source'] != metadata['source'] or row['source'] != metadata['source'] or
                prediction['model_version'] != metadata['model_version'] or not row.get('valid')):
            raise ValueError('Prediction link, model, or provenance mismatch')
        if (prediction['prediction_horizon_seconds'] != 15 or
                (timestamp(prediction['target_timestamp']) - timestamp(prediction['observation_timestamp'])).total_seconds() != 15):
            raise ValueError('Invalid prediction target timestamp/horizon')
        raw, operational = prediction['raw_predicted_utilization'], prediction['predicted_utilization']
        if (not math.isfinite(raw) or operational != min(1, max(0, raw)) or
                not math.isclose(prediction['current_utilization'], row['utilization'])):
            raise ValueError('Prediction value or operational clipping mismatch')
        if (prediction['congestion_threshold'] != metadata['threshold'] or
                prediction['congestion_predicted'] != (operational >= metadata['threshold'])):
            raise ValueError('Invalid congestion threshold/warning')
        checked += 1
        if key in labels:
            label = labels[key]
            pairs.append(dict(prediction, actual_future_utilization=label['target'],
                actual_target_timestamp=label['label_timestamp'],
                actual_horizon_seconds=label['label_elapsed_time'] - label['monotonic_elapsed_time']))
    if not pairs:
        raise ValueError('No valid predictions with matured future observations')
    return dict(source=metadata['source'], status='matched_predictions_checked', checked_predictions=checked,
        model_version=metadata['model_version'], matured_pairs=pairs,
        evaluation=comparison([r['actual_future_utilization'] for r in pairs],
                              [r['raw_predicted_utilization'] for r in pairs],
                              [r['current_utilization'] for r in pairs]),
        prediction_status_counts=dict(Counter(p['status'] for p in predictions)))


def run_research(records, output, source='live_mininet', plots=True):
    if not records or {r.get('source') for r in records} != {source}:
        raise ValueError('No observations or mixed/unexpected provenance')
    identities = [(r['session_id'], r['link_id'], r['monotonic_elapsed_time']) for r in records]
    if len(set(identities)) != len(identities):
        raise ValueError('Duplicate session/link observations')
    examples = build_examples(records)
    train, validation, test = research_split(examples)
    for name, rows, minimum in (('train', train, 60), ('validation', validation, 20), ('test', test, 20)):
        if len(rows) < minimum or not any(r['target'] >= 0.8 for r in rows) or not any(r['target'] < 0.8 for r in rows):
            raise ValueError(f'{name} lacks sufficient examples or both congestion classes; collect more sessions')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'report.json').exists() or (output / 'model.pkl').exists():
        raise ValueError('Research output already exists; do not overwrite a final holdout evaluation')
    selected_sessions = {r['session_id'] for r in train + validation}
    validation_session, holdout_session = validation[0]['session_id'], test[0]['session_id']
    validation_cutoff = min(r['time'] for r in test) - LOOKBACK - TOLERANCE
    selection_records = [r for r in records if r['session_id'] in selected_sessions and
        (r['session_id'] != validation_session or
         timestamp(r['observation_timestamp']).timestamp() < validation_cutoff)]
    candidates = []
    from sklearn.inspection import permutation_importance
    with tempfile.TemporaryDirectory() as directory:
        candidate_path = Path(directory) / 'candidate.pkl'
        for parameters in CANDIDATES:
            metadata = train_live_predictor(selection_records, candidate_path, source=source,
                holdout_session=validation_session, forest_parameters=parameters)
            candidates.append(dict(parameters=parameters, evaluation=metadata['evaluation']))
        selected = min(candidates, key=lambda c: c['evaluation']['random_forest']['mae'])
        train_live_predictor(selection_records, candidate_path, source=source,
            holdout_session=validation_session, forest_parameters=selected['parameters'])
        candidate_model = load_model(candidate_path, source)['model']
        importance = permutation_importance(candidate_model,
            [r['features'] for r in validation], [r['target'] for r in validation],
            scoring='neg_mean_absolute_error', n_repeats=5, random_state=42, n_jobs=1)
    # Refit once on earlier training + validation; the last session is evaluated once.
    metadata = train_live_predictor(records, output / 'model.pkl', source=source,
        holdout_session=holdout_session, forest_parameters=selected['parameters'])
    model = load_model(output / 'model.pkl', source)['model']
    predicted = model.predict([r['features'] for r in test]).tolist()
    actual, current = [r['target'] for r in test], [r['utilization'] for r in test]
    pairs = []
    regimes = {}
    for row, estimate in zip(test, predicted):
        regime = ('approaching_congestion' if row['utilization'] < 0.8 <= row['target'] else
                  'steady' if abs(row['target'] - row['utilization']) <= 0.1 else 'transition')
        pair = dict(session_id=row['session_id'], source=source, link_id=row['link_id'],
            observation_timestamp=row['observation_timestamp'], target_timestamp=row['label_timestamp'],
            nominal_target_timestamp=(timestamp(row['observation_timestamp']) + timedelta(seconds=15)).isoformat(),
            actual_horizon_seconds=row['label_elapsed_time'] - row['monotonic_elapsed_time'],
            current_utilization=row['utilization'], actual_future_utilization=row['target'],
            raw_predicted_utilization=estimate, predicted_utilization=min(1, max(0, estimate)),
            congestion_predicted=estimate >= 0.8, model_version=metadata['model_version'], regime=regime)
        pairs.append(pair)
        regimes.setdefault(regime, []).append(pair)
    regime_reports = {}
    for regime in ('steady', 'approaching_congestion', 'transition'):
        rows = regimes.get(regime, [])
        regime_reports[regime] = comparison([r['actual_future_utilization'] for r in rows],
            [r['raw_predicted_utilization'] for r in rows], [r['current_utilization'] for r in rows]) if rows else None
    link_reports = {}
    for link in sorted({p['link_id'] for p in pairs}):
        rows = [p for p in pairs if p['link_id'] == link]
        link_reports[link] = comparison([r['actual_future_utilization'] for r in rows],
            [r['raw_predicted_utilization'] for r in rows], [r['current_utilization'] for r in rows])
    summaries = []
    for session in sorted({r['session_id'] for r in records}):
        rows = [r for r in records if r['session_id'] == session]
        valid = [r for r in rows if r.get('valid')]
        values = [r['utilization'] for r in valid]
        summaries.append(dict(session_id=session, record_count=len(rows), valid_count=len(valid),
            duration_seconds=max(r['monotonic_elapsed_time'] for r in rows) - min(r['monotonic_elapsed_time'] for r in rows),
            links=sorted({r['link_id'] for r in valid}), statuses=dict(Counter(r['status'] for r in rows)),
            utilization_min=min(values) if values else None, utilization_max=max(values) if values else None,
            utilization_mean=sum(values) / len(values) if values else None,
            positive_sample_count=sum(u >= 0.8 for u in values),
            precongestion_sample_count=sum(0.6 <= u < 0.8 for u in values)))
    report = dict(source=source, status='offline_evaluated_live_inference_unverified',
        sessions=summaries, model_metadata=metadata, selected_parameters=selected['parameters'],
        selection_rule='Lowest validation MAE among four fixed candidates; final holdout never used for selection',
        validation_candidates=candidates, validation_feature_importance={name: dict(
            mae_increase_mean=float(mean), std=float(std)) for name, mean, std in zip(
                FEATURES, importance.importances_mean, importance.importances_std)},
        split=dict(train_count=len(train), validation_count=len(validation), test_count=len(test),
            final_refit_count=metadata['train_count'], validation_session=validation_session,
            holdout_session=holdout_session, train_positive_count=sum(r['target'] >= 0.8 for r in train),
            validation_positive_count=sum(r['target'] >= 0.8 for r in validation),
            test_positive_count=sum(r['target'] >= 0.8 for r in test)),
        test=comparison(actual, predicted, current), test_regimes=regime_reports, test_links=link_reports,
        limitations=['Link samples and overlapping windows are correlated; counts are not independent congestion events',
            'Permutation importance is validation-only and correlated features can obscure contributions',
            '15-second endpoint forecasting does not estimate exact congestion onset or routing benefit',
            'Controlled UDP offered load differs from measured wire throughput; inspect iperf3 and telemetry together',
            'No accuracy or live-inference claim follows from synthetic or offline-only evaluation'])
    for name, value in (('report.json', report), ('model.metadata.json', metadata)):
        (output / name).write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    with (output / 'holdout-pairs.jsonl').open('w') as handle:
        for pair in pairs:
            handle.write(json.dumps(pair, allow_nan=False) + '\n')
    if plots:
        plot_results(report, pairs, output)
    return report


def plot_results(report, pairs, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter
    source = report['source']
    links = sorted({p['link_id'] for p in pairs})
    figure, axes = plt.subplots(len(links), 1, figsize=(12, max(3, len(links) * 2.5)), squeeze=False)
    for link, axis in zip(links, axes[:, 0]):
        rows = [p for p in pairs if p['link_id'] == link]
        times = [timestamp(p['target_timestamp']) for p in rows]
        for field, label in (('actual_future_utilization', 'Actual'), ('raw_predicted_utilization', 'RF'),
                             ('current_utilization', 'Persistence')):
            axis.plot(times, [p[field] for p in rows], label=label, linewidth=1)
        axis.axhline(0.8, color='red', linestyle=':', label='80% threshold')
        axis.set_ylabel(link + '\nUtilization ratio')
        axis.xaxis.set_major_formatter(DateFormatter('%H:%M:%S'))
    axes[0, 0].legend(ncol=4)
    axes[-1, 0].set_xlabel('Target time (UTC); nominal horizon +15 seconds')
    figure.suptitle(f'Independent holdout — source: {source}')
    figure.tight_layout()
    for extension in ('png', 'svg'):
        figure.savefig(output / f'holdout-timeseries.{extension}', dpi=160)
    plt.close(figure)
    names, values, deviations = [], [], []
    for name, entry in report['validation_feature_importance'].items():
        names.append(name)
        values.append(entry['mae_increase_mean'])
        deviations.append(entry['std'])
    figure, axis = plt.subplots(figsize=(9, 5))
    axis.barh(names, values, xerr=deviations)
    axis.set_xlabel('Validation MAE increase after permutation (five repeats)')
    axis.set_title(f'Validation feature sensitivity — source: {source}')
    figure.tight_layout()
    for extension in ('png', 'svg'):
        figure.savefig(output / f'validation-features.{extension}', dpi=160)
    plt.close(figure)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('databases', nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', choices=('live_mininet', 'synthetic', 'simulated_openflow'), default='live_mininet')
    parser.add_argument('--audit-model', type=Path, help='Audit stored inference from this frozen trusted model instead of training')
    args = parser.parse_args()
    try:
        records = [row for db in args.databases for row in read_rows(db, 'observations')]
        if args.audit_model:
            metadata = load_model(args.audit_model, args.source)['metadata']
            predictions = [row for db in args.databases for row in read_rows(db, 'predictions')]
            report = audit_predictions(records, predictions, metadata)
            args.output.mkdir(parents=True, exist_ok=False)
            (args.output / 'audit.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
        else:
            report = run_research(records, args.output, args.source)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
