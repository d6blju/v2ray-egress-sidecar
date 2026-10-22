"""Real sidecar hot-reload/rollback against isolated localhost echo exits."""
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'deploy'))
from sidecar_route_transaction import RouteTransaction, RouteError, atomic, read_text


class EchoConnect(socketserver.BaseRequestHandler):
    def handle(self):
        header = b''
        while b'\r\n\r\n' not in header:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            header += chunk
        self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
        while True:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            self.request.sendall(self.server.label + chunk)


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@unittest.skipUnless(os.environ.get('SIDECAR_TEST_CHECK_BIN'), 'requires real Rust sidecar binary')
class LiveRouteTests(unittest.TestCase):
    def test_established_connections_survive_apply_and_failed_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            servers = [Proxy(('127.0.0.1', 0), EchoConnect) for _ in range(2)]
            for server, label in zip(servers, (b'A:', b'B:')):
                server.label = label
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
            with socket.socket() as reserve:
                reserve.bind(('127.0.0.1', 0))
                port = reserve.getsockname()[1]
            config = os.path.join(directory, 'config.toml')
            state = os.path.join(directory, 'state.json')
            initial = ('listen="127.0.0.1:%d"\nreload_interval_secs=1\nstats_path="%s"\n'
                       '[[outbounds]]\ntag="old"\nkind="http_connect"\naddr="127.0.0.1:%d"\n'
                       % (port, os.path.join(directory, 'stats.jsonl'), servers[0].server_address[1]))
            candidate = initial + ('[[outbounds]]\ntag="new"\nkind="http_connect"\naddr="127.0.0.1:%d"\n'
                                   '[[rules]]\ndomain_suffix=".route-test.example"\noutbound="new"\n' % servers[1].server_address[1])
            atomic(config, initial)
            route = RouteTransaction(config, state, directory)
            binary = os.environ['SIDECAR_TEST_CHECK_BIN']
            logpath = os.path.join(directory, 'stderr.log')
            with open(logpath, 'wb') as log:
                child = subprocess.Popen([binary, '--config', config], stdout=log, stderr=log)
                try:
                    def connected():
                        stream = socket.create_connection(('127.0.0.1', port), timeout=3)
                        stream.sendall(b'\x05\x01\x00')
                        self.assertEqual(stream.recv(2), b'\x05\x00')
                        host = b'www.route-test.example'
                        stream.sendall(b'\x05\x01\x00\x03' + bytes([len(host)]) + host + b'\x01\xbb')
                        reply = b''
                        while len(reply) < 10:
                            reply += stream.recv(10 - len(reply))
                        self.assertEqual(reply[1], 0)
                        return stream
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            old = connected()
                            break
                        except OSError:
                            if time.monotonic() > deadline:
                                raise
                            time.sleep(.05)
                    with old:
                        def echoed(stream, label):
                            stream.sendall(b'ping')
                            data = b''
                            while len(data) < 6:
                                data += stream.recv(6 - len(data))
                            self.assertEqual(data, label + b'ping')
                        def checkpoint():
                            return os.path.getsize(logpath)
                        def confirm(cursor, text):
                            end = time.monotonic() + 5
                            while time.monotonic() < end:
                                with open(logpath, 'rb') as source:
                                    source.seek(cursor)
                                    messages = source.read()
                                if b'config reloaded successfully' in messages:
                                    return {'confirmed': True, 'pid': child.pid}
                                time.sleep(.05)
                            raise RuntimeError('real sidecar failed to reload')
                        def validate(text):
                            path = os.path.join(directory, 'candidate.toml')
                            atomic(path, text)
                            subprocess.run([binary, '--check', '--config', path], check=True, stdout=subprocess.PIPE)
                        echoed(old, b'A:')
                        route.execute(initial, candidate, {'active': True}, validate, checkpoint, confirm)
                        echoed(old, b'A:')
                        with connected() as new:
                            echoed(new, b'B:')
                            def failed_postcheck():
                                raise RuntimeError('injected failure after clear reload')
                            with self.assertRaises(RouteError):
                                route.execute(candidate, initial, {'active': False}, validate, checkpoint, confirm, failed_postcheck)
                            echoed(old, b'A:')
                            echoed(new, b'B:')
                            with connected() as restored:
                                echoed(restored, b'B:')
                            self.assertEqual(read_text(config), candidate)
                            self.assertIsNone(child.poll())
                finally:
                    child.terminate()
                    child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
