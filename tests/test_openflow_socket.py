"""Real controller process/TCP codec test with simulated switch peers, NOT Mininet."""

import queue
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from os_ken.lib.packet import ethernet, packet
from os_ken.ofproto import ofproto_v1_3 as ofp, ofproto_v1_3_parser as parser

from topology_config import HOSTS, SWITCHES, wired_links


class SwitchPeer:
    def __init__(self, dpid, port):
        self.dpid = dpid
        self.socket = socket.create_connection(('127.0.0.1', port), timeout=2)
        self.socket.settimeout(0.5)
        self.lock = threading.Lock()
        self.outputs = queue.Queue()
        self.errors = []
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        self.send(ofp.OFPT_HELLO)

    def send(self, kind, body=b'', xid=1):
        with self.lock:
            self.socket.sendall(struct.pack('!BBHI', 4, kind, 8 + len(body), xid) + body)

    def port(self, number, up=True):
        return struct.pack(ofp.OFP_PORT_PACK_STR, number, b'\x02\0\0\0\0\0',
                           f's{self.dpid}-eth{number}'.encode(), 0,
                           0 if up else ofp.OFPPS_LINK_DOWN, 0, 0, 0, 0, 0, 0)

    def run(self):
        buffer = b''
        try:
            while not self.stopped.is_set():
                try:
                    chunk = self.socket.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buffer += chunk
                while len(buffer) >= 8:
                    version, kind, length, xid = struct.unpack_from('!BBHI', buffer)
                    if len(buffer) < length:
                        break
                    body, buffer = buffer[8:length], buffer[length:]
                    if version != 4:
                        raise AssertionError(f'Unexpected OpenFlow version {version}')
                    if kind == ofp.OFPT_FEATURES_REQUEST:
                        self.send(ofp.OFPT_FEATURES_REPLY, struct.pack(
                            ofp.OFP_SWITCH_FEATURES_PACK_STR, self.dpid, 0, 254, 0, 0, 0), xid)
                    elif kind == ofp.OFPT_MULTIPART_REQUEST:
                        subtype = struct.unpack_from('!H', body)[0]
                        if subtype == ofp.OFPMP_PORT_DESC:
                            ports = {1}
                            for link in wired_links():
                                for end in ('src', 'dst'):
                                    if link[end] == self.dpid:
                                        ports.add(link[end + '_port'])
                            self.send(ofp.OFPT_MULTIPART_REPLY,
                                      struct.pack('!HH4x', subtype, 0) +
                                      b''.join(self.port(p) for p in sorted(ports)), xid)
                            self.ready.set()
                    elif kind == ofp.OFPT_BARRIER_REQUEST:
                        self.send(ofp.OFPT_BARRIER_REPLY, xid=xid)
                    elif kind == ofp.OFPT_ECHO_REQUEST:
                        self.send(ofp.OFPT_ECHO_REPLY, body, xid)
                    elif kind == ofp.OFPT_PACKET_OUT:
                        actions_len = struct.unpack_from('!H', body, 8)[0]
                        ports = [struct.unpack_from('!I', body, offset + 4)[0]
                                 for offset in range(16, 16 + actions_len, 16)]
                        self.outputs.put(ports)
        except (OSError, AssertionError) as error:
            if not self.stopped.is_set():
                self.errors.append(str(error))

    def inject(self, src, dst):
        frame = packet.Packet()
        frame.add_protocol(ethernet.ethernet(src=HOSTS[src]['mac'], dst=HOSTS[dst]['mac'], ethertype=0x0806))
        frame.serialize()
        match = bytearray()
        parser.OFPMatch(in_port=1).serialize(match, 0)
        body = struct.pack(ofp.OFP_PACKET_IN_PACK_STR, ofp.OFP_NO_BUFFER,
                           len(frame.data), ofp.OFPR_NO_MATCH, 0, 0)
        self.send(ofp.OFPT_PACKET_IN, body + bytes(match) + b'\0\0' + frame.data)

    def change_port(self, number, up):
        self.send(ofp.OFPT_PORT_STATUS, struct.pack('!B7x', ofp.OFPPR_MODIFY) + self.port(number, up))

    def close(self):
        self.stopped.set()
        self.socket.close()
        self.thread.join(timeout=2)


class SocketTests(unittest.TestCase):
    def test_six_real_openflow_handshakes_forwarding_failure_and_recovery(self):
        # Ephemeral port keeps this test isolated from the user's live controller.
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        peers = []
        with tempfile.TemporaryFile(mode='w+') as log:
            process = subprocess.Popen([
                sys.executable, '-u', 'controller.py', '--ofp-listen-host', '127.0.0.1',
                '--ofp-tcp-listen-port', str(port)], cwd=Path(__file__).resolve().parents[1],
                stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 8
                while True:
                    try:
                        peers = [SwitchPeer(1, port)]
                        break
                    except ConnectionRefusedError:
                        if time.monotonic() >= deadline or process.poll() is not None:
                            self.fail('Controller failed to listen')
                        time.sleep(0.05)
                peers += [SwitchPeer(i, port) for i in list(SWITCHES)[1:]]
                self.assertTrue(all(peer.ready.wait(timeout=5) for peer in peers))
                source = peers[1]

                def expect_output(allowed):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        source.inject(2, 5)
                        try:
                            if source.outputs.get(timeout=0.2) in allowed:
                                return
                        except queue.Empty:
                            pass
                    self.fail(f'No expected PacketOut {allowed}')

                expect_output([[4]])
                source.change_port(4, False)
                expect_output([[2], [3]])
                source.change_port(4, True)
                expect_output([[4]])
                self.assertTrue(all(not peer.errors for peer in peers))
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
                for i in SWITCHES:
                    self.assertIn(f'Validated static port map: DPID={i}', output)


if __name__ == '__main__':
    unittest.main()
