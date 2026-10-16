import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from prediction import build_examples, feature_for, chronological_split, evaluate, Predictor, load_model
from traffic_engine import train_live_predictor


def observations(count=180, session='experiment', irregular=False):
    start = datetime(2026, 10, 15, tzinfo=timezone.utc)
    result = []
    for i in range(count):
        elapsed = i * 5 + (0.3 if irregular and i % 2 else 0)
        utilization = 0.5 + 0.45 * math.sin(i / 8)
        result.append(dict(session_id=session, source='synthetic', schema_version=1,
            observation_timestamp=(start + timedelta(seconds=elapsed)).isoformat(),
            monotonic_elapsed_time=elapsed, link_id='s2-s5', capacity_mbps=200,
            utilization=utilization, tx_mbps=utilization * 200, rx_mbps=5,
            valid=True, status='valid', link_active=True))
    return result


class FeatureTests(unittest.TestCase):
    def test_lags_and_real_future_target(self):
        rows = observations(10)
        examples = build_examples(rows)
        self.assertEqual(len(examples), 4)
        self.assertEqual(examples[0]['features'][:4], [rows[i]['utilization'] for i in (3, 2, 1, 0)])
        self.assertEqual(examples[0]['target'], rows[6]['utilization'])
        self.assertEqual(examples[0]['label_elapsed_time'] - examples[0]['monotonic_elapsed_time'], 15)

    def test_no_future_information_and_missing_target_excluded(self):
        rows = observations(8)
        before = feature_for(rows[:4])
        rows[4]['utilization'] = 999
        self.assertEqual(feature_for(rows[:4]), before)
        self.assertEqual(build_examples(rows[:6]), [])

    def test_irregular_alignment_and_missing_window_break(self):
        self.assertTrue(build_examples(observations(12, irregular=True)))
        rows = observations(12)
        rows[5].update(valid=False, utilization=None, status='missing')
        examples = build_examples(rows)
        self.assertFalse(any(r['monotonic_elapsed_time'] < 25 < r['label_elapsed_time'] for r in examples))

    def test_sessions_are_never_joined_and_out_of_order_is_rejected(self):
        self.assertEqual(build_examples(observations(4, 'a') + observations(4, 'b')), [])
        rows = observations(10)
        rows[4], rows[5] = rows[5], rows[4]
        with self.assertRaises(ValueError):
            build_examples(rows)

    def test_chronological_split_purges_overlapping_labels_and_windows(self):
        train, test = chronological_split(build_examples(observations()))
        self.assertLess(max(r['label_time'] for r in train), min(r['time'] for r in test) - 15)
        self.assertLess(max(r['time'] for r in train), min(r['time'] for r in test))

    def test_regression_and_congestion_metrics_and_no_positive_events(self):
        metrics = evaluate([0, 1, 1, 0], [0.1, 0.9, 0.1, 0.9], 0.8)
        self.assertAlmostEqual(metrics['mae'], 0.5)
        self.assertAlmostEqual(metrics['rmse'], math.sqrt(0.41))
        self.assertEqual(metrics['congestion_precision'], 0.5)
        self.assertEqual(metrics['false_alarm_rate'], 0.5)
        self.assertEqual(metrics['positive_event_count'], 2)
        no_events = evaluate([0.1, 0.2], [0.9, 0.9], 0.8)
        self.assertIsNone(no_events['congestion_f1'])
        self.assertIsNone(no_events['false_alarm_rate'])


class ModelTests(unittest.TestCase):
    def test_training_round_trip_baseline_and_reproducibility(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'model.pkl'
            report = train_live_predictor(observations(), path, source='synthetic')
            self.assertIn('persistence', report['evaluation'])
            model = load_model(path, source='synthetic')
            first = Predictor(model).predict(observations(10))
            self.assertEqual(first['status'], 'valid')
            self.assertEqual(first['prediction_horizon_seconds'], 15)
            self.assertEqual(datetime.fromisoformat(first['target_timestamp']) -
                datetime.fromisoformat(first['observation_timestamp']), timedelta(seconds=15))
            second_path = Path(directory) / 'model2.pkl'
            train_live_predictor(observations(), second_path, source='synthetic')
            self.assertEqual(Predictor(load_model(second_path, 'synthetic')).predict(observations(10))[
                'predicted_utilization'], first['predicted_utilization'])
            with self.assertRaises(ValueError):
                load_model(path, source='live_mininet')

    def test_no_model_insufficient_history_and_inference_failure(self):
        self.assertEqual(Predictor().predict([])['status'], 'empty_history')
        self.assertEqual(Predictor().predict(observations(10))['status'], 'model_not_ready')
        class Broken:
            def predict(self, values):
                raise RuntimeError('model failed')
        bundle = dict(model=Broken(), metadata=dict(model_version='broken', threshold=0.8))
        predictor = Predictor(bundle)
        self.assertEqual(predictor.predict(observations(2))['status'], 'insufficient_history')
        self.assertEqual(predictor.predict(observations(10))['status'], 'inference_error')

    def test_operational_clipping_preserves_raw_output_and_threshold(self):
        class AboveCapacity:
            def predict(self, values):
                return [1.5]
        result = Predictor(dict(model=AboveCapacity(), metadata=dict(model_version='fixture',
            threshold=0.8, source='synthetic'))).predict(observations(8))
        self.assertEqual(result['raw_predicted_utilization'], 1.5)
        self.assertEqual(result['predicted_utilization'], 1)
        self.assertTrue(result['congestion_predicted'])

    def test_no_labels_insufficient_samples_and_provenance_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'model.pkl'
            for rows in ([], observations(5), observations(20)):
                with self.assertRaises(ValueError):
                    train_live_predictor(rows, path, source='synthetic')
            with self.assertRaises(ValueError):
                train_live_predictor(observations(), path, source='live_mininet')

    def test_model_identifier_changes_with_warning_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'model.pkl'
            first = train_live_predictor(observations(), path, source='synthetic', threshold=0.8)
            second = train_live_predictor(observations(), path, source='synthetic', threshold=0.9)
            self.assertNotEqual(first['model_version'], second['model_version'])


if __name__ == '__main__':
    unittest.main()
