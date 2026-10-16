import unittest

from analyze_phase3_validation import diagnostic_examples
from test_prediction import observations


class DiagnosticTests(unittest.TestCase):
    def test_alternative_horizons_use_real_future_labels_and_causal_extended_history(self):
        rows = observations(30, irregular=True)
        examples = diagnostic_examples(rows, horizon=10)
        self.assertTrue(examples)
        first = examples[0]
        self.assertLessEqual(abs(first['label_elapsed_time'] - first['monotonic_elapsed_time'] - 10), 2)
        original = first['extended_features'][:]
        for row in rows:
            if row['monotonic_elapsed_time'] > first['monotonic_elapsed_time']:
                row['utilization'] = 100
        after = diagnostic_examples(rows, horizon=10)[0]
        self.assertEqual(after['extended_features'], original)
        self.assertNotEqual(after['target'], first['target'])

    def test_missing_history_breaks_diagnostic_windows(self):
        rows = observations(30)
        rows[15].update(valid=False, utilization=None)
        examples = diagnostic_examples(rows, horizon=15)
        self.assertFalse(any(r['monotonic_elapsed_time'] < 75 < r['label_elapsed_time'] for r in examples))
        self.assertFalse(any(75 < r['monotonic_elapsed_time'] < 135 for r in examples))


if __name__ == '__main__':
    unittest.main()
