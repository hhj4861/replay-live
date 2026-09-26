"""Bounded CONNECT relay for one administrator-run proxy experiment.

Credentials stay in memory. TLS remains end-to-end between yt-dlp and YouTube.
The byte counter includes CONNECT and tunnel traffic in both directions, but
not TCP/IP framing; use 100 MB per run and verify provider usage before retrying.
"""
import base64
import select
import socket
import socketserver
import threading
from urllib.parse import unquote, urlsplit


def allowed_authority(authority):
    host, separator, port = authority.lower().rpartition(':')
    return (separator and port == '443' and
            any(host == domain or host.endswith('.' + domain)
                for domain in ('youtube.com', 'googlevideo.com', 'ytimg.com')))


class BudgetProxy:
    def __init__(self, upstream, limit=100_000_000):
        parsed = urlsplit(upstream)
        if (parsed.scheme != 'http' or parsed.hostname != 'gw.dataimpulse.com' or
                parsed.port != 823 or not parsed.username or not parsed.password or
                parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            raise ValueError('PROXY_CONFIGURATION_REQUIRED')
        self.address = (parsed.hostname, parsed.port)
        self.authorization = base64.b64encode(
            (unquote(parsed.username) + ':' + unquote(parsed.password)).encode())
        self.limit = limit
        self.transferred = 0
        self.exhausted = threading.Event()
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(4)
        self.connections = set()
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                if not owner.slots.acquire(blocking=False):
                    return
                try:
                    owner.relay(self.request)
                except (OSError, ValueError):
                    pass  # Never log requests, upstream errors or credentials.
                finally:
                    owner.slots.release()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self.server = Server(('127.0.0.1', 0), Handler)

    def close_connections(self):
        with self.lock:
            connections = tuple(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def account(self, count):
        with self.lock:
            if self.exhausted.is_set() or self.transferred + count > self.limit:
                self.exhausted.set()
                return False
            self.transferred += count
            return True

    def relay(self, client):
        client.settimeout(10)
        request = bytearray()
        while not request.endswith(b'\r\n\r\n'):
            value = client.recv(1)
            if not value or len(request) >= 8192:
                return
            request.extend(value)
        fields = request.split(b'\r\n', 1)[0].decode('ascii').split()
        if len(fields) != 3 or fields[0] != 'CONNECT' or not allowed_authority(fields[1]):
            client.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
            return
        if self.exhausted.is_set():
            return
        with socket.create_connection(self.address, timeout=10) as upstream:
            with self.lock:
                self.connections.update((client, upstream))
            try:
                header = ('CONNECT ' + fields[1] + ' HTTP/1.1\r\nHost: ' + fields[1] +
                          '\r\nProxy-Authorization: Basic ').encode() + self.authorization + b'\r\n\r\n'
                if not self.account(len(header)):
                    return
                upstream.sendall(header)
                response = bytearray()
                while not response.endswith(b'\r\n\r\n'):
                    chunk = upstream.recv(1)
                    if not chunk or len(response) >= 8192 or not self.account(len(chunk)):
                        return
                    response.extend(chunk)
                if response.split(b' ', 2)[1] != b'200':
                    client.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
                    return
                client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                while not self.exhausted.is_set():
                    readable, _, _ = select.select([client, upstream], [], [], 10)
                    if not readable:
                        return
                    for source in readable:
                        data = source.recv(16_384)
                        if not data or not self.account(len(data)):
                            return
                        (upstream if source is client else client).sendall(data)
            finally:
                with self.lock:
                    self.connections.difference_update((client, upstream))
                if self.exhausted.is_set():
                    self.close_connections()

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def __exit__(self, *_):
        self.exhausted.set()
        self.close_connections()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
