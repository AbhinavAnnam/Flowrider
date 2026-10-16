"""Actual OS-Ken socket polling against simulated counters; no real Mininet traffic."""

import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from os_ken.ofproto import ofproto_v1_3 as ofp
from telemetry_store import read_rows
from topology_config import SWITCHES, wired_links
from test_openflow_socket import SwitchPeer


class StatsPeer(SwitchPeer):
    def __init__(self, dpid, port):
        self.origin = time.monotonic()
        super().__init__(dpid, port)

    def multipart_request(self, subtype, body, xid):
        if subtype != ofp.OFPMP_PORT_STATS:
            return
        elapsed = time.monotonic() - self.origin
        ports = {1}
        for link in wired_links():
            for end in ('src', 'dst'):
                if link[end] == self.dpid:
                    ports.add(link[end + '_port'])
        stats = []
        for port in sorted(ports):
            byte_count = int(elapsed * 5_000_000)  # Explicitly simulated 40 Mbps counters.
            stats.append(struct.pack(ofp.OFP_PORT_STATS_PACK_STR, port,
                100, 100, byte_count, byte_count, 0, 0, 0, 0, 0, 0, 0, 0,
                int(elapsed), int((elapsed % 1) * 1e9)))
        self.send(ofp.OFPT_MULTIPART_REPLY, struct.pack('!HH4x', subtype, 0) + b''.join(stats), xid)


class TelemetrySocketTests(unittest.TestCase):
    def test_real_polling_storage_and_disconnect_with_simulated_peers(self):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'wire.sqlite'
            peers = []
            with tempfile.TemporaryFile(mode='w+') as log:
                process = subprocess.Popen([sys.executable, '-u', 'controller.py',
                    '--ofp-listen-host', '127.0.0.1', '--ofp-tcp-listen-port', str(port),
                    '--telemetry-enabled', '--telemetry-source', 'simulated_openflow',
                    '--telemetry-db', str(db)], stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            peers = [StatsPeer(1, port)]
                            break
                        except ConnectionRefusedError:
                            if time.monotonic() > deadline or process.poll() is not None:
                                self.fail('Controller failed to start')
                            time.sleep(0.05)
                    peers += [StatsPeer(i, port) for i in list(SWITCHES)[1:]]
                    deadline = time.monotonic() + 17
                    while time.monotonic() < deadline:
                        if db.exists():
                            try:
                                rows = read_rows(db, 'observations')
                            except sqlite3.OperationalError as error:
                                if 'no such table' not in str(error):
                                    raise
                                rows = []  # The background worker is still initializing schema.
                            valid = [row for row in rows if row['valid'] and row['link_id'] == 's2-s5']
                            if valid:
                                self.assertAlmostEqual(valid[-1]['utilization'], 0.2, delta=0.03)
                                self.assertEqual(valid[-1]['capacity_mbps'], 200)
                                self.assertEqual(valid[-1]['source'], 'simulated_openflow')
                                break
                        time.sleep(0.1)
                    else:
                        self.fail('No valid two-ended OpenFlow measurements')
                    peers[1].close()
                    deadline = time.monotonic() + 8  # Disconnect inspection has the nominal 5s cadence.
                    while time.monotonic() < deadline:
                        rows = [r for r in read_rows(db, 'predictions') if r['link_id'] == 's2-s5']
                        if rows and rows[-1]['status'] == 'inactive':
                            break
                        time.sleep(0.1)
                    else:
                        log.flush()
                        log.seek(0)
                        self.fail('Disconnected link retained an active prediction\n' + log.read())
                    self.assertIsNone(process.poll())
                finally:
                    for peer in peers:
                        peer.close()
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    log.seek(0)
                    output = log.read()
                    self.assertNotIn('Traceback', output, output)
                    self.assertNotIn('ERROR:', output, output)


if __name__ == '__main__':
    unittest.main()
