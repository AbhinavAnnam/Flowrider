import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock,patch

try:
    import integration_phase6 as smoke
except ModuleNotFoundError as error:
    if error.name!='mininet':raise
    smoke=None


@unittest.skipIf(smoke is None,'Run with /usr/bin/python3 for Ubuntu Mininet')
class ModeSmokeTests(unittest.TestCase):
    def setUp(self):
        self.args=SimpleNamespace(routing_mode='baseline',database='unused')
        self.route=dict(source_mac='00:00:00:00:00:01',destination_mac='00:00:00:00:00:04',
            baseline_path=[1,2,5,4],installed_route=dict(path=[1,2,5,4]),observed_route=dict(path=[1,2,5,4]))
        self.snapshot=dict(routing_mode='baseline',trial_id='smoke-trial',topology_version='version',
            topology=dict(links=[dict(active=True,routing_weight=2,base_delay_ms=2)]),routes=[self.route])
        self.state=dict(source='live_mininet',stale=False,snapshot=self.snapshot)

    def test_smoke_refuses_wrong_controller_mode_before_traffic(self):
        self.snapshot['routing_mode']='predictive_ml';network=MagicMock()
        with patch.object(smoke,'read_routing_state',return_value=self.state):
            with self.assertRaisesRegex(RuntimeError,'mode differs'):smoke.checks(network,self.args,{})
        network.pingAll.assert_not_called()

    def test_smoke_rejects_nonstatic_weights_before_claiming_verification(self):
        self.snapshot['topology']['links'][0]['routing_weight']=100
        network=MagicMock();network.pingAll.return_value=0
        with patch.object(smoke,'read_routing_state',return_value=self.state), \
             patch.object(smoke,'eventually'),patch.object(smoke,'verify_path') as verify:
            with self.assertRaisesRegex(RuntimeError,'Static weights differ'):
                smoke.checks(network,self.args,{})
        verify.assert_not_called()
