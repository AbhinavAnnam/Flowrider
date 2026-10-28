"""Synthetic forecasts only: no Mininet, model writes, or live results."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import test_phase6c_four_switch as fixtures
from test_predictive_socket import Fabric


class ControlledForecastTests(unittest.TestCase):
    def test_baseline_control_and_foreground_detour(self):
        fixtures.FourSwitchForwardingPolicy().test_qualifying_forecast_policy_contract_and_bidirectional_packet_forwarding()

    def test_both_measured_pairs_need_two_forecasts_then_forward_bidirectionally(self):
        for pair,source_switch,destination_port,baseline,detour in (
                ((1,2),1,1,[1,2,4],[1,3,4]),
                ((3,4),2,4,[2,4],[2,1,3,4])):
            with self.subTest(pair=pair):
                harness=fixtures.FourSwitchForwardingPolicy().harness('predictive_ml')
                harness.a,harness.b=(f'00:00:00:00:00:{i:02x}' for i in pair)
                harness.router.ensure(harness.a,harness.b);harness.finish()
                self.assertEqual(harness.pair()['installed']['path'],baseline)
                harness.now+=31;harness.telemetry(['s2-s4'])  # synthetic 0.95, others 0.05
                self.assertEqual(harness.pair()['installed']['path'],baseline)
                graph=harness.router._weighted()
                cost=lambda nodes:sum(graph[u][v]['ml_weight'] for u,v in zip(nodes,nodes[1:]))
                self.assertLess(cost(detour),.9*cost(baseline))
                harness.now+=5;harness.telemetry(['s2-s4']);harness.finish()
                self.assertEqual(harness.pair()['installed']['path'],detour)
                fabric=Fabric('phase6c')
                fabric.peers={d:SimpleNamespace(rules={}) for d in harness.dps}
                for index,rule in enumerate(harness.pair()['installed']['rules']):
                    fabric.peers[rule['dpid']].rules[index]=dict(priority=rule['priority'],match=rule['match'],
                        actions=harness.router.actions(rule,harness.dps[rule['dpid']]),packets=0)
                self.assertEqual(fabric.forward(source_switch,harness.a,harness.b),detour)
                self.assertEqual(fabric.forward(4,harness.b,harness.a,ingress=destination_port),list(reversed(detour)))
                self.assertFalse(fabric.errors)


if __name__=='__main__':unittest.main(verbosity=2)
