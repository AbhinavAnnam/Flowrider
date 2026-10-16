import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from telemetry_store import TelemetryStore
from test_prediction import observations


class CLITests(unittest.TestCase):
    def test_matured_prediction_evaluation_matches_persistence_pairs(self):
        from phase3 import evaluate_predictions
        rows = observations(12)
        predictions = [dict(row, model_version='fixture', status='valid',
            congestion_threshold=0.8, current_utilization=row['utilization'],
            raw_predicted_utilization=row['utilization']) for row in rows[3:8]]
        report = evaluate_predictions(rows, predictions)[0]
        self.assertEqual(report['random_forest']['sample_count'], 5)
        self.assertEqual(report['random_forest'], report['persistence'])

    def test_training_inspection_and_export_preserve_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            db, model = directory / 'experiment.sqlite', directory / 'rf.pkl'
            with TelemetryStore(db, source='synthetic', config={}) as store:
                store.write_batch([], [], observations(), [])
            result = subprocess.run([sys.executable, 'phase3.py', 'train', str(db),
                '--model', str(model), '--source', 'synthetic'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            metadata = json.loads(result.stdout)
            self.assertEqual(metadata['source'], 'synthetic')
            self.assertTrue(Path(str(model) + '.features.jsonl').exists())
            self.assertTrue(Path(str(model) + '.labels.jsonl').exists())
            output = directory / 'observations.csv'
            result = subprocess.run([sys.executable, 'phase3.py', 'export', str(db),
                '--table', 'observations', '--output', str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('synthetic', output.read_text())
            result = subprocess.run([sys.executable, 'phase3.py', 'sessions', str(db)], capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)[0]['source'], 'synthetic')

    def test_missing_database_is_an_error_and_does_not_create_data(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'missing.sqlite'
            result = subprocess.run([sys.executable, 'phase3.py', 'inspect', str(db)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(db.exists())


if __name__ == '__main__':
    unittest.main()
