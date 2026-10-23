"""Run the real shell launcher with owned fixture processes, without sudo/lab."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


class Phase5LauncherTests(unittest.TestCase):
    def run_launcher(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'scripts').mkdir()
            (root/'bin').mkdir()
            (root/'data').mkdir()
            (root/'model.pkl').touch()
            shutil.copyfile(Path(__file__).resolve().parents[1]/'scripts/run_phase5_validation.sh',
                            root/'scripts/run_phase5_validation.sh')
            fixture = root/'fixture.py'
            fixture.write_text('''import json,os,signal,sys,time
from pathlib import Path
root=Path(__file__).parent
args=sys.argv[1:]
if args[0]=='-':
    sys.stdin.read()  # Port probe/readiness fixture; never bind a real port.
    sys.exit(0)
if args[0]=='-u':
    (root/'controller.pid').write_text(str(os.getpid()))
    def stop(*unused):
        (root/'controller.stopped').touch()
        sys.exit(0)
    signal.signal(signal.SIGTERM,stop)
    while True: time.sleep(.01)
if args[0]=='routing_state.py':
    print(json.dumps(dict(status='fixture_snapshot')))
    sys.exit(0)
if args[0]=='-v': sys.exit(0)
output=Path(args[args.index('--output')+1])
def interrupted(*unused):
    (root/'validator.stopped').touch()
    (output/'acceptance.json').write_text(json.dumps(dict(status='failed_live_mininet')))
    print('fixture Mininet stopped',flush=True)
    sys.exit(143)
signal.signal(signal.SIGTERM,interrupted)
(root/'validator.pid').write_text(str(os.getpid()))
print('fixture traffic measurements preserved',flush=True)
if os.environ['PHASE5_FIXTURE_MODE']=='interrupt':
    while True: time.sleep(.01)
status=7 if os.environ['PHASE5_FIXTURE_MODE']=='failure' else 0
(output/'acceptance.json').write_text(json.dumps(dict(status='failed_live_mininet' if status else 'passed_live_mininet')))
sys.exit(status)
''')
            # Both fake executables exec their fixture so signals reach the
            # tracked PID just as sudo forwards them to its monitored command.
            for name in ('sudo', 'python'):
                path = root/'bin'/name
                path.write_text('#!/usr/bin/env bash\nexec ' + shutil.which(sys.executable) +
                                ' "$(dirname "$0")/../fixture.py" "$@"\n')
                path.chmod(0o755)
            (root/'bin/iperf3').symlink_to('/bin/true')
            environment = dict(os.environ, PATH=str(root/'bin')+':'+os.environ['PATH'],
                FLOWRIDER_PYTHON=str(root/'bin/python'), PHASE5_FIXTURE_MODE=mode)
            child = subprocess.Popen(['bash', str(root/'scripts/run_phase5_validation.sh'), 'model.pkl'],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                if mode == 'interrupt':
                    end = time.monotonic()+5
                    while not (root/'validator.pid').exists() and time.monotonic()<end:
                        time.sleep(.01)
                    self.assertTrue((root/'validator.pid').exists(), 'validator did not start')
                    child.send_signal(signal.SIGTERM)
                stdout, stderr = child.communicate(timeout=15)
                expected = {'success': 0, 'failure': 7, 'interrupt': 143}[mode]
                self.assertEqual(child.returncode, expected, stdout+stderr)
                evidence, = (root/'data').iterdir()
                self.assertEqual(json.loads((evidence/'final-routing-state.json').read_text())['status'],
                                 'fixture_snapshot')
                self.assertIn('fixture traffic measurements preserved', (evidence/'mininet.log').read_text())
                self.assertTrue((root/'controller.stopped').exists())
                for pidfile in ('controller.pid', 'validator.pid'):
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int((root/pidfile).read_text()), 0)
                if mode == 'interrupt':
                    self.assertTrue((root/'validator.stopped').exists())
                    self.assertIn('fixture Mininet stopped', (evidence/'mininet.log').read_text())
                if mode == 'success':
                    self.assertIn('Live acceptance passed', stdout)
                else:
                    self.assertNotIn('Live acceptance passed', stdout)
                    self.assertIn('Live validation failed', stderr)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)
                child.stdout.close()
                child.stderr.close()
                # A failing assertion must not leave a fixture controller alive.
                for pidfile in ('controller.pid', 'validator.pid'):
                    if (root/pidfile).exists():
                        try:os.kill(int((root/pidfile).read_text()), signal.SIGKILL)
                        except ProcessLookupError:pass

    def test_failed_validation_preserves_exit_code_logs_and_snapshot(self):
        self.run_launcher('failure')

    def test_interrupt_reaches_validator_and_waits_for_cleanup(self):
        self.run_launcher('interrupt')

    def test_success_reaps_controller_and_logger(self):
        self.run_launcher('success')


if __name__ == '__main__':
    unittest.main()
