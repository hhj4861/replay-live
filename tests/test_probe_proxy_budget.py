"""Loopback-only relay integration; no provider account or YouTube requests."""
import importlib.util
from pathlib import Path
import socket
import socketserver
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('budget', Path(__file__).resolve().parents[1] / 'scripts/probe_proxy_budget.py')
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        header = bytearray()
        try:
            while not header.endswith(b'\r\n\r\n'):
                data = self.request.recv(1)
                if not data:
                    return
                header.extend(data)
            self.server.headers.append(bytes(header))
            self.request.sendall(b'HTTP/1.1 200 OK\r\n\r\n')
            while data := self.request.recv(16384):
                self.request.sendall(data)
        except OSError:
            pass


class ProxyBudgetTests(unittest.TestCase):
    def test_authority_cannot_redirect_to_arbitrary_hosts_or_ports(self):
        for authority in ['youtube.com:443', 'rr1.googlevideo.com:443', 'www.youtube.com:443']:
            self.assertTrue(budget.allowed_authority(authority))
        for authority in ['youtube.com.evil.test:443', 'localhost:443', '127.0.0.1:443',
                          'youtube.com:80', 'youtube.com:443/path', 'evil-youtube.com:443']:
            self.assertFalse(budget.allowed_authority(authority))

    def test_bidirectional_tunnel_count_cap_and_cleanup(self):
        original_connect = socket.create_connection
        with socketserver.ThreadingTCPServer(('127.0.0.1', 0), Echo) as upstream:
            upstream.daemon_threads = True
            upstream.headers = []
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            def connect(address, **kwargs):
                self.assertEqual(address, ('gw.dataimpulse.com', 823))
                return original_connect(upstream.server_address, **kwargs)
            try:
                with patch.object(budget.socket, 'create_connection', side_effect=connect):
                    with budget.BudgetProxy('http://probe:TEST_SECRET@gw.dataimpulse.com:823', limit=1024) as proxy:
                        with original_connect(proxy.server.server_address, timeout=2) as client:
                            client.sendall(b'CONNECT www.youtube.com:443 HTTP/1.1\r\n\r\n')
                            self.assertEqual(client.recv(8192), b'HTTP/1.1 200 Connection Established\r\n\r\n')
                            before = proxy.transferred
                            client.sendall(b'hello')
                            self.assertEqual(client.recv(5), b'hello')
                            self.assertEqual(proxy.transferred - before, 10)
                            client.sendall(b'x' * 1500)
                            self.assertEqual(client.recv(1), b'')
                            self.assertTrue(proxy.exhausted.is_set())
                            self.assertLessEqual(proxy.transferred, proxy.limit)
                        self.assertIn(b'Proxy-Authorization: Basic ', upstream.headers[0])
                    self.assertFalse(proxy.thread.is_alive())
                    self.assertEqual(proxy.connections, set())
            finally:
                upstream.shutdown()
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
