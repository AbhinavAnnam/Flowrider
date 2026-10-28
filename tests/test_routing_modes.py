"""Explicit Phase 6 policy selection and session provenance; no live lab."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from controller import resolve_routing_mode
from telemetry_runtime import TelemetryWorker
from topology_config import TOPOLOGY_VERSION
from collect_phase3 import traffic_schedule,validate_traffic_configuration
from types import SimpleNamespace


class RoutingModeTests(unittest.TestCase):
    def test_collection_rejects_a_mismatched_recorded_traffic_plan(self):
        args=SimpleNamespace(seconds=600,seed=42,bandwidth_scale=.1)
        plan=dict(generator='collect_phase3',seconds=600,seed=42,bandwidth_scale=.1,
                  stages=traffic_schedule(600,42),flow_pairs=[['h1','h4'],['h3','h6']],measurement_interval_seconds=5)
        for mode in ('baseline','predictive_ml'):
            config=dict(routing_mode=mode,traffic_configuration=plan)
            validate_traffic_configuration(config,args)
            with self.assertRaisesRegex(RuntimeError,'Traffic configuration'):
                validate_traffic_configuration(config,SimpleNamespace(seconds=600,seed=43,bandwidth_scale=.1))
        validate_traffic_configuration({},args)  # Preserve existing Phase 3 sessions.

    def test_defaults_and_legacy_flag_are_backward_compatible(self):
        self.assertIsNone(resolve_routing_mode(False,None))
        self.assertEqual(resolve_routing_mode(True,None),'predictive_ml')
        for mode in ('baseline','predictive_ml'):
            self.assertEqual(resolve_routing_mode(False,mode),mode)
        self.assertEqual(resolve_routing_mode(True,'predictive_ml'),'predictive_ml')
        with self.assertRaises(ValueError):resolve_routing_mode(True,'baseline')
        with self.assertRaises(ValueError):resolve_routing_mode(False,'unknown')

    def test_session_metadata_distinguishes_trials_without_changing_sampler(self):
        for mode in ('baseline','predictive_ml'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'state.sqlite'
                metadata=dict(routing_mode=mode,trial_id=mode+'-trial',topology_version=TOPOLOGY_VERSION,
                              traffic_configuration=dict(seed=42,seconds=60,flows=[['h1','h4'],['h3','h6']]))
                worker=TelemetryWorker(path,experiment_config=metadata);worker.close()
                self.assertIsNone(worker.failure)
                with sqlite3.connect(path) as c:
                    config=json.loads(c.execute('SELECT config_json FROM sessions').fetchone()[0])
                self.assertEqual(config['routing_mode'],mode)
                self.assertEqual(config['trial_id'],mode+'-trial')
                self.assertEqual(config['traffic_configuration']['seed'],42)
                self.assertEqual(config['topology_version'],TOPOLOGY_VERSION)
                self.assertEqual(config['interval_seconds'],5)
                self.assertEqual(config['bandwidth_scale'],.1)
