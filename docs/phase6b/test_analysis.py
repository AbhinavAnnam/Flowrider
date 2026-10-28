"""Focused arithmetic/interpretation regressions for the offline report."""
import unittest
from analyze import flow_metrics,aggregate_flows,exposure,change_counts,delta


class AnalysisTests(unittest.TestCase):
    def test_goodput_uses_bytes_and_receiver_duration_not_mean_stage_rates(self):
        rows=[dict(sent_bytes=100,received_bytes=90,sender_seconds=1,receiver_seconds=2,lost_packets=1,packets=10),
              dict(sent_bytes=900,received_bytes=900,sender_seconds=9,receiver_seconds=9,lost_packets=0,packets=90)]
        result=aggregate_flows(rows)
        self.assertAlmostEqual(result['sender_mbps'],800/1e6)
        self.assertAlmostEqual(result['receiver_mbps'],990*8/11/1e6)
        self.assertEqual(result['loss_percent'],1)

    def test_missing_receiver_summary_is_an_error_not_zero_loss(self):
        with self.assertRaisesRegex(ValueError,'receiver'):
            flow_metrics(dict(start=dict(test_start=dict(protocol='UDP')),end=dict(sum_sent={})))

    def test_exposure_uses_preceding_intervals_clips_window_and_does_not_fill_gaps(self):
        rows=[dict(time=0,elapsed=0,utilization=.1,valid=True),dict(time=5,elapsed=5,utilization=.9,valid=True),
              dict(time=10,elapsed=10,utilization=.1,valid=True),dict(time=30,elapsed=30,utilization=1,valid=True)]
        result=exposure(rows,2,12)
        self.assertEqual(result['coverage_seconds'],8)
        self.assertEqual(result['congested_link_seconds'],3)
        self.assertAlmostEqual(result['mean_utilization'],(3*.9+5*.1)/8)

    def test_initial_installs_and_refreshes_are_not_route_switches(self):
        events=[dict(kind='route_activated',path=[1,2],previous_path=None,reason='initial_baseline'),
                dict(kind='route_activated',path=[1,2],previous_path=[1,2],reason='refresh_hard_timeout'),
                dict(kind='route_activated',path=[1,3,2],previous_path=[1,2],reason='predicted_congestion')]
        self.assertEqual(change_counts(events),dict(path_changes=1,predictive_changes=1,refreshes=1,initial_installs=1))

    def test_percentage_change_from_zero_is_unavailable(self):
        self.assertEqual(delta(0,1),dict(absolute=1,percent=None))


if __name__=='__main__':unittest.main()
