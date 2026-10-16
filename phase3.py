"""Offline Phase 3 training, inspection, export and matured-prediction evaluation."""

import argparse
import csv
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from telemetry_store import SOURCES, TABLES, read_rows


def sessions(path):
    with sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute('SELECT * FROM sessions ORDER BY started_timestamp')]


def export_rows(rows, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='') as handle:
        if path.suffix == '.jsonl':
            for row in rows:
                handle.write(json.dumps(row, allow_nan=False) + '\n')
        elif rows:
            fields = sorted({field for row in rows for field in row})
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v
                                 for k, v in row.items()})


def evaluate_predictions(observations, predictions):
    from collections import defaultdict
    from prediction import build_examples, evaluate
    labels = {(r['session_id'], r['link_id'], r['observation_timestamp']): r
              for r in build_examples(observations)}
    groups = defaultdict(list)
    for prediction in predictions:
        key = (prediction['session_id'], prediction['link_id'], prediction['observation_timestamp'])
        label = labels.get(key)
        if prediction['status'] == 'valid' and label is not None:
            groups[prediction['source'], prediction['model_version']].append((label, prediction))
    results = []
    for (source, version), pairs in sorted(groups.items()):
        thresholds = {p['congestion_threshold'] for _, p in pairs}
        if len(thresholds) != 1:
            raise ValueError('One model version has inconsistent thresholds')
        threshold = thresholds.pop()
        actual = [row['target'] for row, _ in pairs]
        results.append(dict(source=source, model_version=version, threshold=threshold,
            random_forest=evaluate(actual, [p['raw_predicted_utilization'] for _, p in pairs], threshold),
            persistence=evaluate(actual, [p['current_utilization'] for _, p in pairs], threshold)))
    if not results:
        raise ValueError('No valid predictions with matured, aligned future targets')
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    train = commands.add_parser('train', help='Train offline; no automatic retraining')
    train.add_argument('databases', nargs='+')
    train.add_argument('--model', required=True)
    train.add_argument('--source', choices=SOURCES, default='live_mininet')
    train.add_argument('--threshold', type=float, default=0.8)
    train.add_argument('--test-fraction', type=float, default=0.2)
    train.add_argument('--holdout-session')
    train.add_argument('--min-train', type=int, default=60)
    train.add_argument('--min-test', type=int, default=20)
    inspect = commands.add_parser('inspect', help='Latest per-link values, or recent history')
    inspect.add_argument('database')
    inspect.add_argument('--table', choices=TABLES, default='predictions')
    inspect.add_argument('--session')
    inspect.add_argument('--limit', type=int, default=7)
    inspect.add_argument('--history', action='store_true')
    listing = commands.add_parser('sessions')
    listing.add_argument('database')
    export = commands.add_parser('export')
    export.add_argument('database')
    export.add_argument('--table', choices=TABLES, required=True)
    export.add_argument('--output', required=True)
    export.add_argument('--session')
    evaluate = commands.add_parser('evaluate', help='Compare stored predictions with later actual observations')
    evaluate.add_argument('database')
    evaluate.add_argument('--session', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'train':
            from prediction import build_examples, chronological_split
            from traffic_engine import train_live_predictor
            records = [row for db in args.databases for row in read_rows(db, 'observations')]
            identities = [(r['session_id'], r['link_id'], r['monotonic_elapsed_time']) for r in records]
            if len(identities) != len(set(identities)):
                raise ValueError('Duplicate session/link observations; do not supply the same dataset twice')
            metadata = train_live_predictor(records, args.model, source=args.source,
                threshold=args.threshold, min_train=args.min_train, min_test=args.min_test,
                test_fraction=args.test_fraction, holdout_session=args.holdout_session)
            examples = build_examples(records)
            train_rows, test_rows = chronological_split(examples, args.test_fraction, args.holdout_session)
            key = lambda r: (r['session_id'], r['link_id'], r['time'])
            train_keys, test_keys = {key(r) for r in train_rows}, {key(r) for r in test_rows}
            features, labels = [], []
            for index, row in enumerate(examples):
                common = dict(example_id=index, session_id=row['session_id'], source=row['source'],
                    link_id=row['link_id'], observation_timestamp=row['observation_timestamp'],
                    partition='train' if key(row) in train_keys else 'test' if key(row) in test_keys else 'purged')
                features.append(dict(common, features=row['features']))
                labels.append(dict(common, target=row['target'], label_timestamp=row['label_timestamp'],
                                   label_elapsed_time=row['label_elapsed_time']))
            export_rows(features, args.model + '.features.jsonl')
            export_rows(labels, args.model + '.labels.jsonl')
            Path(args.model + '.metadata.json').write_text(json.dumps(metadata, indent=2, allow_nan=False) + '\n')
            print(json.dumps(metadata, indent=2, allow_nan=False))
        elif args.command == 'sessions':
            print(json.dumps(sessions(args.database), indent=2))
        elif args.command == 'inspect':
            if args.limit < 1:
                raise ValueError('--limit must be positive')
            session = args.session or (sessions(args.database)[-1]['session_id'])
            rows = read_rows(args.database, args.table, session)
            if not args.history:
                latest = {}
                for row in rows:
                    key = (row.get('link_id'), row.get('source_dpid'), row.get('source_port'))
                    latest[key] = row
                rows = list(latest.values())
                from prediction import timestamp
                for row in rows:
                    age = (datetime.now(timezone.utc) - timestamp(row['observation_timestamp'])).total_seconds()
                    row['inspection_age_seconds'] = age
                    row['inspection_status'] = 'stale' if age > 10 else 'recent'
            print(json.dumps(rows[-args.limit:], indent=2, allow_nan=False))
        elif args.command == 'export':
            export_rows(read_rows(args.database, args.table, args.session), args.output)
        elif args.command == 'evaluate':
            result = evaluate_predictions(read_rows(args.database, 'observations', args.session),
                                          read_rows(args.database, 'predictions', args.session))
            print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, OSError, sqlite3.Error, KeyError, IndexError) as error:
        parser.exit(1, f'Phase 3: {error}\n')


if __name__ == '__main__':
    main()
