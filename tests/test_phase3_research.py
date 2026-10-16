import copy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from test_prediction import observations
from prediction import load_model, timestamp, Predictor
from research_phase3 import research_split, run_research, audit_predictions
from collect_phase3 import traffic_schedule


def independent_sessions():
    rows = []
    for day, session in enumerate(('training', 'validation', 'holdout')):
        group = observations(100, session)
        for row in group:
            row['observation_timestamp'] = (timestamp(row['observation_timestamp']) +
                                            timedelta(days=day)).isoformat()
        rows.extend(group)
    return rows


class ResearchTests(unittest.TestCase):
    def test_inference_audit_preserves_matured_pairs_and_checks_timestamp_and_warning(self):
        class Persistence:
            def predict(self, features):
                return [features[0][0]]
        rows = observations(15)
        metadata = dict(source='synthetic', model_version='frozen', threshold=0.8,
                        train_sessions=['other-training-session'])
        predictor = Predictor(dict(model=Persistence(), metadata=metadata))
        predictions = [predictor.predict(rows[:i + 1]) for i in range(3, 15)]
        result = audit_predictions(rows, predictions, metadata)
        self.assertEqual(len(result['matured_pairs']), 9)
        self.assertEqual(result['evaluation']['random_forest'], result['evaluation']['persistence'])
        predictions[0]['target_timestamp'] = predictions[0]['observation_timestamp']
        with self.assertRaisesRegex(ValueError, 'timestamp'):
            audit_predictions(rows, predictions, metadata)
        predictions[0] = predictor.predict(rows[:4])
        predictions[0]['congestion_predicted'] = not predictions[0]['congestion_predicted']
        with self.assertRaisesRegex(ValueError, 'threshold'):
            audit_predictions(rows, predictions, metadata)

    def test_split_uses_whole_independent_sessions_and_purges_boundaries(self):
        from prediction import build_examples
        train, validation, test = research_split(build_examples(independent_sessions()))
        self.assertEqual({r['session_id'] for r in train}, {'training'})
        self.assertEqual({r['session_id'] for r in validation}, {'validation'})
        self.assertEqual({r['session_id'] for r in test}, {'holdout'})
        self.assertLess(max(r['label_time'] for r in train), min(r['time'] for r in validation) - 17)
        with self.assertRaisesRegex(ValueError, 'three'):
            research_split(build_examples(observations()))

    def test_overlapping_sessions_and_duplicate_records_are_rejected(self):
        from prediction import build_examples
        rows = observations(100, 'a') + observations(100, 'b') + observations(100, 'c')
        with self.assertRaisesRegex(ValueError, 'overlap'):
            research_split(build_examples(rows))
        with tempfile.TemporaryDirectory() as directory:
            rows = independent_sessions()
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                run_research(rows + [rows[0]], Path(directory), source='synthetic', plots=False)

    def test_validation_selects_model_without_holdout_tuning_and_saves_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = independent_sessions()
            report = run_research(rows, Path(directory) / 'first', source='synthetic', plots=False)
            changed = copy.deepcopy(rows)
            for row in changed:
                if row['session_id'] == 'holdout':
                    row['utilization'] = 1 - row['utilization']
                    row['tx_mbps'] = row['utilization'] * row['capacity_mbps']
            second = run_research(changed, Path(directory) / 'second', source='synthetic', plots=False)
            self.assertEqual(report['selected_parameters'], second['selected_parameters'])
            self.assertEqual(report['validation_candidates'], second['validation_candidates'])
            self.assertEqual(report['split']['test_count'], 94)
            self.assertEqual(report['source'], 'synthetic')
            self.assertIn('approaching_congestion', report['test_regimes'])
            self.assertEqual(report['test_links']['s2-s5']['random_forest']['sample_count'], 94)
            self.assertEqual(len(report['validation_feature_importance']), 10)
            model = load_model(Path(directory) / 'first' / 'model.pkl', 'synthetic')
            self.assertEqual(model['metadata']['holdout_session'], 'holdout')
            pairs = [json.loads(line) for line in (Path(directory) / 'first' / 'holdout-pairs.jsonl').read_text().splitlines()]
            self.assertEqual(len(pairs), 94)
            self.assertTrue(all(timestamp(p['target_timestamp']) > timestamp(p['observation_timestamp']) for p in pairs))

    def test_collection_schedule_is_reproducible_varied_and_bounded(self):
        schedule = traffic_schedule(610, 17)
        self.assertEqual(schedule, traffic_schedule(610, 17))
        self.assertNotEqual(schedule, traffic_schedule(610, 29))
        self.assertEqual(sum(s['seconds'] for s in schedule), 610)
        self.assertTrue({'low', 'medium', 'high', 'ramp', 'burst', 'recovery'} <= {s['scenario'] for s in schedule})
        self.assertTrue(all(0 <= s['rate_mbps'] <= 95 and s['seconds'] > 0 for s in schedule))


if __name__ == '__main__':
    unittest.main()
