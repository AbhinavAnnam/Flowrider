"""No root or live network needed: exercise the live harness with timed processes."""
import json
import io
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

try:
    import integration_phase5 as live
except ModuleNotFoundError as error:
    if error.name != 'mininet':
        raise
    live = None


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def measurement(duration=40):
    return dict(start=dict(test_start=dict(duration=duration)), intervals=[dict(sum=dict(bytes=35000000))],
                end=dict(sum_sent=dict(seconds=duration, bytes=35000000),
                         sum_received=dict(seconds=duration, bytes=35000000)))


class Process:
    next_pid = 10000000

    def __init__(self, clock, args, finish=None, ignore_signals=False):
        self.clock, self.args, self.finish = clock, args, finish
        self.returncode = None
        self.ignore_signals = ignore_signals
        self.signals = []
        self.pid = Process.next_pid
        self.stdout = None
        Process.next_pid += 1

    def poll(self):
        if self.finish is not None and self.clock.now >= self.finish and self.returncode is None:
            self.returncode = 0
        return self.returncode

    def wait(self, timeout):
        if self.poll() is None:
            if self.finish is not None and self.finish <= self.clock.now + timeout:
                self.clock.now = self.finish
                self.returncode = 0
            else:
                self.clock.sleep(timeout)
                raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def send_signal(self, value):
        self.signals.append(value)
        if value == signal.SIGKILL or not self.ignore_signals:
            self.returncode = -value

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def communicate(self, timeout):
        self.wait(timeout)
        return b'ESTAB iperf3 control socket', None


@unittest.skipIf(live is None, 'Run with /usr/bin/python3 for Ubuntu Mininet')
class Phase5LifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.args = SimpleNamespace(output=Path(temporary.name), database=Path(temporary.name)/'telemetry.sqlite')
        self.clock = Clock()
        timer = patch.object(live, 'time', SimpleNamespace(monotonic=self.clock.monotonic,
                    time=self.clock.monotonic, sleep=self.clock.sleep))
        timer.start()
        self.addCleanup(timer.stop)

    def network(self, finishes=(40.2, None), documents=None, launch_failure=False, stubborn=False):
        clock, processes, handles = self.clock, [], []
        documents = documents or (measurement(), measurement())

        class Host:
            def __init__(self, name):
                self.name = name

            def IP(self):
                return '10.0.0.' + self.name[1:]

            def popen(self, args, **kwargs):
                handle = kwargs.get('stdout')
                if hasattr(handle, 'write'):
                    handles.append(handle)
                finish = None
                if '-c' in args:
                    index = 0 if self.name == 'h1' else 1
                    if launch_failure and index == 1:
                        raise OSError('second client could not launch')
                    document = documents[index]
                    handle.write(document if isinstance(document, str) else json.dumps(document))
                    kwargs['stderr'].write('client diagnostic stderr')
                    finish = None if finishes[index] is None else clock.now + finishes[index]
                process = Process(clock, args, finish, ignore_signals=stubborn)
                if args[0] == 'ss':
                    process.finish = clock.now
                    process.stdout = io.BytesIO()
                processes.append(process)
                return process

        net = {name: Host(name) for name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')}
        network = MagicMock()
        network.__getitem__.side_effect = net.__getitem__
        network.pingAll.return_value = 0
        return network, processes, handles

    def stage(self, network, observe=lambda: None):
        stage = dict(index=0, rate_mbps=7, duration_seconds=40)
        live.run_stage(network, self.args, stage, observe)
        return stage

    def test_timeout_records_both_clients_and_cleans_all_owned_processes(self):
        network, processes, handles = self.network()
        with patch.object(live, 'eventually'), \
             patch.object(live, 'read_routing_state', return_value=dict(source='live_mininet', stale=True)):
            with self.assertRaisesRegex(RuntimeError, 'traffic|stage'):
                live.checks(network, self.args)
        stages = json.loads((self.args.output/'traffic-stages.json').read_text())
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]['status'], 'failed')
        self.assertEqual(len(stages[0]['client_exit_codes']), 2)
        clients = stages[0]['clients']
        self.assertEqual(clients[0]['status'], 'passed')
        self.assertEqual(clients[1]['status'], 'timed_out')
        self.assertIn('diagnostics', clients[1])
        self.assertEqual(clients[1]['stderr_tail'], 'client diagnostic stderr')
        self.assertTrue(all(p.poll() is not None for p in processes))
        self.assertTrue(all(h.closed for h in handles))
        cleanup = json.loads((self.args.output/'cleanup.json').read_text())
        self.assertTrue(cleanup['cleaned_up'])
        for name in ('00-h1-h4.json', '00-h3-h6.json'):
            self.assertEqual(json.loads((self.args.output/name).read_text()), measurement())

    def test_healthy_clients_get_full_duration_plus_setup_and_result_allowance(self):
        network, processes, handles = self.network(finishes=(49, 49.5))
        stage = self.stage(network)
        self.assertEqual(stage['status'], 'passed')
        self.assertEqual(stage['client_exit_codes'], [0, 0])
        self.assertEqual(self.clock.now, 49.5)
        self.assertTrue(all(not p.signals for p in processes))
        self.assertTrue(all(h.closed for h in handles))
        self.assertTrue(all('--connect-timeout' in c['command'] for c in stage['clients']))

    def test_two_stuck_clients_share_launch_based_deadlines(self):
        network, processes, _ = self.network(finishes=(None, None))
        stage = self.stage(network)
        self.assertEqual([c['status'] for c in stage['clients']], ['timed_out', 'timed_out'])
        self.assertEqual(self.clock.now, 50)  # No second full duration or grace period.
        self.assertTrue(all(p.poll() is not None for p in processes))

    def test_incomplete_measurements_fail_even_when_exit_code_is_zero(self):
        partial = measurement(20)
        iperf_error = dict(measurement(), error='control connection closed')
        no_receiver = measurement()
        no_receiver['end']['sum_received']['bytes'] = 0
        short_receiver = measurement()
        short_receiver['end']['sum_received']['seconds'] = 20
        for document in ('{partial json', '[]', {}, partial, iperf_error, no_receiver, short_receiver):
            with self.subTest(document=document):
                network, _, _ = self.network(finishes=(40, 40), documents=(measurement(), document))
                stage = self.stage(network)
                self.assertEqual(stage['client_exit_codes'], [0, 0])
                self.assertEqual(stage['status'], 'failed')
                self.assertEqual(stage['clients'][1]['status'], 'incomplete')
                self.assertFalse(stage['clients'][1]['measurements_complete'])

    def test_observer_failure_preserves_stage_and_cleans_both_clients(self):
        network, processes, handles = self.network()
        stage = dict(index=0, rate_mbps=7, duration_seconds=40)
        def observe():
            raise RuntimeError('snapshot failure')
        with self.assertRaisesRegex(RuntimeError, 'snapshot failure'):
            live.run_stage(network, self.args, stage, observe)
        self.assertEqual(stage['status'], 'failed')
        self.assertTrue(all(c['status'] == 'aborted' for c in stage['clients']))
        self.assertTrue(all(p.poll() is not None for p in processes))
        self.assertTrue(all(h.closed for h in handles))

    def test_partial_launch_failure_cleans_first_client(self):
        network, processes, handles = self.network(launch_failure=True)
        stage = dict(index=0, rate_mbps=7, duration_seconds=40)
        with self.assertRaisesRegex(OSError, 'could not launch'):
            live.run_stage(network, self.args, stage, lambda: None)
        self.assertEqual(stage['status'], 'failed')
        self.assertEqual(len(stage['clients']), 1)
        self.assertTrue(all(p.poll() is not None for p in processes))
        self.assertTrue(all(h.closed for h in handles))

    def test_targeted_stage_uses_cross_traffic_and_background_probe_rates(self):
        network, processes, _ = self.network(finishes=(40,40))
        stage=dict(index=0,rate_mbps=85,duration_seconds=40,
                   flows=[('h1','h2',85),('h1','h4',5)])
        live.run_stage(network,self.args,stage,lambda:None)
        self.assertEqual(stage['status'],'passed')
        commands=[p.args for p in processes]
        self.assertEqual([c[c.index('-c')+1] for c in commands],['10.0.0.2','10.0.0.4'])
        self.assertEqual([c[c.index('-b')+1] for c in commands],['85M','5M'])

    def test_low_load_recovery_requires_fresh_safe_forecast_and_actual_forwarding(self):
        route=dict(source_mac='00:00:00:00:00:01',destination_mac='00:00:00:00:00:04',
                   installed_route=dict(path=[1,6,5,4]))
        state=dict(stale=False,source='live_mininet',snapshot=dict(routes=[route],topology=dict(links=[dict(
            link_id='s1-s2',fresh=True,measurement=dict(valid=True,utilization=.12),
            prediction=dict(status='valid',predicted_utilization=.2))])))
        with patch.object(live,'read_routing_state',return_value=state), \
             patch.object(live,'verify_path',return_value=dict(delivery='ping_passed')) as verify:
            result=live.verify_low_load_recovery(None,self.args)
            self.assertEqual(result['forwarding']['delivery'],'ping_passed')
            verify.assert_called_once_with(None,self.args,route,require_alternative=False)
            for change in ('stale','high_prediction','missing_prediction','high_measurement'):
                value=json.loads(json.dumps(state));link=value['snapshot']['topology']['links'][0]
                if change=='stale':value['stale']=True
                if change=='high_prediction':link['prediction']['predicted_utilization']=.9
                if change=='missing_prediction':link['prediction']['predicted_utilization']=None
                if change=='high_measurement':link['measurement']['utilization']=.9
                with self.subTest(change=change), patch.object(live,'read_routing_state',return_value=value):
                    with self.assertRaisesRegex(RuntimeError,'Low-load recovery'):
                        live.verify_low_load_recovery(None,self.args)
            verify.side_effect=RuntimeError('No positive per-hop readback')
            with self.assertRaisesRegex(RuntimeError,'positive per-hop'):
                live.verify_low_load_recovery(None,self.args)

    def test_stubborn_children_do_not_prevent_other_cleanup(self):
        network, processes, _ = self.network(finishes=(None, None), stubborn=True)
        stage = self.stage(network)
        self.assertEqual(stage['status'], 'failed')
        self.assertTrue(all(p.poll() is not None for p in processes))
        for client in stage['clients']:
            self.assertEqual(client['cleanup']['signals'], ['SIGINT', 'SIGTERM', 'SIGKILL'])
            self.assertEqual(client['exit_code'], -signal.SIGKILL)

    def test_diagnostic_timeout_is_bounded_and_does_not_replace_client_failure(self):
        network, processes, _ = self.network()
        diagnostic = Process(self.clock, ['ss'], ignore_signals=True)
        diagnostic.stdout = io.BytesIO()
        original = network.__getitem__.side_effect
        # Fail only the diagnostic socket command; normal traffic still launches.
        class DiagnosticHost:
            def __init__(self, host):
                self.host = host
            def IP(self):
                return self.host.IP()
            def popen(self, args, **kwargs):
                return diagnostic if args[0] == 'ss' else self.host.popen(args, **kwargs)
        network.__getitem__.side_effect = lambda name: DiagnosticHost(original(name))
        stage = self.stage(network)
        self.assertEqual(stage['clients'][1]['status'], 'timed_out')
        self.assertIsNotNone(diagnostic.poll())
        self.assertTrue(diagnostic.stdout.closed)
        self.assertLessEqual(self.clock.now, 60)
        self.assertIn('error', stage['clients'][1]['diagnostics']['sockets']['h3'])

    def test_main_records_teardown_failure_instead_of_success(self):
        def run_network(args, checks):
            checks(None)
            raise RuntimeError('net.stop failed')
        with patch.object(live.sys, 'argv', ['integration_phase5.py', '--database', str(self.args.database),
                '--output', str(self.args.output)]), \
             patch.object(live, 'run_network', side_effect=run_network), \
             patch.object(live, 'checks', return_value=dict(applied_route='proof')), \
             patch.object(live, 'preflight'), patch.object(live.shutil, 'which', return_value='/usr/bin/iperf3'):
            with self.assertRaisesRegex(RuntimeError, 'net.stop failed'):
                live.main()
        acceptance = json.loads((self.args.output/'acceptance.json').read_text())
        self.assertEqual(acceptance['status'], 'failed_live_mininet')
        self.assertIn('net.stop failed', acceptance['error'])

    def test_sigterm_runs_teardown_and_records_failure(self):
        stopped = []
        def run_network(args, checks):
            try:signal.raise_signal(signal.SIGTERM)
            finally:stopped.append(True)
        with patch.object(live.sys, 'argv', ['integration_phase5.py', '--database', str(self.args.database),
                '--output', str(self.args.output)]), \
             patch.object(live, 'run_network', side_effect=run_network), \
             patch.object(live.shutil, 'which', return_value='/usr/bin/iperf3'):
            with self.assertRaises(SystemExit) as raised:
                live.main()
        self.assertEqual(raised.exception.code, 143)
        self.assertTrue(stopped)
        acceptance = json.loads((self.args.output/'acceptance.json').read_text())
        self.assertEqual(acceptance['status'], 'failed_live_mininet')

    def test_post_stop_preflight_failure_cannot_report_success(self):
        with patch.object(live.sys, 'argv', ['integration_phase5.py', '--database', str(self.args.database),
                '--output', str(self.args.output)]), \
             patch.object(live, 'run_network'), \
             patch.object(live, 'preflight', side_effect=RuntimeError('remaining lab interface')), \
             patch.object(live.shutil, 'which', return_value='/usr/bin/iperf3'):
            with self.assertRaisesRegex(RuntimeError, 'remaining lab interface'):
                live.main()
        acceptance = json.loads((self.args.output/'acceptance.json').read_text())
        self.assertEqual(acceptance['status'], 'failed_live_mininet')

    def test_real_process_flushes_json_on_interrupt_and_is_reaped(self):
        import sys
        child = subprocess.Popen([sys.executable, '-u', '-c',
            "import signal,time,sys; signal.signal(signal.SIGINT, lambda *a: (print('{\"end\":{}}'), sys.exit(0))); "
            "print('ready'); time.sleep(30)"], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            result = live.stop_process(child)
            self.assertTrue(result['cleaned_up'])
            self.assertEqual(result['exit_code'], 0)
            self.assertEqual(json.loads(child.stdout.read()), dict(end={}))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            child.stdout.close()


if __name__ == '__main__':
    unittest.main()
