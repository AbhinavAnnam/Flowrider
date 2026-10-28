"""Real OS-Ken startup / SQLite metadata, without Mininet or fabricated live traffic."""
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


class ExperimentCapacitySocketTests(unittest.TestCase):
    def test_opt_in_override_reaches_real_controller_snapshot_and_telemetry_config(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory,tempfile.TemporaryFile(mode='w+') as log:
            database=Path(directory)/'state.sqlite'
            process=subprocess.Popen([sys.executable,'-u','controller.py','--ofp-listen-host','127.0.0.1',
                '--ofp-tcp-listen-port',str(port),'--routing-mode','baseline','--telemetry-enabled',
                '--telemetry-source','simulated_openflow','--telemetry-db',str(database),
                '--experiment-link-capacities','{"s2-s5":50}'],stdout=log,stderr=subprocess.STDOUT)
            try:
                deadline=time.monotonic()+10;snapshot=None
                while time.monotonic()<deadline and process.poll() is None:
                    if database.is_file():
                        try:
                            with sqlite3.connect(database) as c:
                                row=c.execute('SELECT record_json FROM routing_snapshots ORDER BY id DESC LIMIT 1').fetchone()
                                if row:
                                    snapshot=json.loads(row[0]);break
                        except sqlite3.OperationalError:pass
                    time.sleep(.05)
                log.seek(0);self.assertIsNotNone(snapshot,log.read())
                self.assertEqual(next(l for l in snapshot['topology']['links'] if l['link_id']=='s2-s5')['capacity_mbps'],50)
                self.assertTrue(all(l['routing_weight'] is None for l in snapshot['topology']['links']))  # disconnected
                with sqlite3.connect(database) as c:config=json.loads(c.execute('SELECT config_json FROM sessions').fetchone()[0])
                self.assertEqual(config['experiment_link_capacities_mbps'],{'s2-s5':50})
            finally:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)


if __name__=='__main__':unittest.main()

class FourSwitchControllerSocketTests(unittest.TestCase):
    def test_selected_topology_reaches_real_controller_and_database(self):
        from topology_config import topology_definition
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory,tempfile.TemporaryFile(mode='w+') as log:
            database=Path(directory)/'state.sqlite'
            process=subprocess.Popen([sys.executable,'-u','controller.py','--ofp-listen-host','127.0.0.1',
                '--ofp-tcp-listen-port',str(port),'--topology','phase6c','--routing-mode','baseline',
                '--telemetry-enabled','--telemetry-db',str(database)],stdout=log,stderr=subprocess.STDOUT)
            try:
                deadline=time.monotonic()+10;snapshot=None
                while time.monotonic()<deadline and process.poll() is None:
                    if database.is_file():
                        try:
                            with sqlite3.connect(database) as c:
                                row=c.execute('SELECT record_json FROM routing_snapshots ORDER BY id DESC LIMIT 1').fetchone()
                                if row:snapshot=json.loads(row[0]);break
                        except sqlite3.OperationalError:pass
                    time.sleep(.05)
                log.seek(0);self.assertIsNotNone(snapshot,log.read())
                self.assertEqual(snapshot['topology_version'],topology_definition('phase6c')[2])
                self.assertEqual(len(snapshot['topology']['links']),4)
                self.assertEqual(len(snapshot['topology']['hosts']),4)
                self.assertEqual(next(l for l in snapshot['topology']['links'] if l['link_id']=='s2-s4')['capacity_mbps'],20)
                with sqlite3.connect(database) as c:config=json.loads(c.execute('SELECT config_json FROM sessions').fetchone()[0])
                self.assertEqual(config['topology_version'],snapshot['topology_version'])
            finally:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
