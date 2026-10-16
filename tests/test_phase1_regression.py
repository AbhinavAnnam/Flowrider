import unittest

from network_topology import build_network_graph
from traffic_engine import generate_historical_telemetry, train_traffic_predictor, update_dynamic_weights


class SimulationTests(unittest.TestCase):
    def test_original_synthetic_training_and_prediction_still_work(self):
        features, labels = generate_historical_telemetry()
        self.assertEqual(features.shape, (99, 2))
        self.assertEqual(labels.shape, (99,))
        first, second = train_traffic_predictor(), train_traffic_predictor()
        self.assertEqual(first.predict([[12, 0.8]]).tolist(), second.predict([[12, 0.8]]).tolist())

    def test_original_weight_equation_and_conceptual_capacity_are_preserved(self):
        class ConstantModel:
            def predict(self, values):
                return [0.9]
        graph = build_network_graph()
        update_dynamic_weights(graph, ConstantModel(), 12)
        self.assertEqual(graph['Router_2']['Router_5']['capacity'], 2000)
        self.assertEqual(graph['Router_2']['Router_5']['latency'], round(2 * (1 + 25 * 0.9 ** 4), 2))
