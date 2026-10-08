"""Shared fixtures for the RetroBridge Phase 1 test suite.

Everything here is offline: local sockets on loopback only, no external DNS,
no live Internet access.
"""
from __future__ import annotations

import io
import json
import socket
import ssl
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pytest

from core.loader import load_device_adapter, load_service_adapter
from core.profiles import ProfileRegistry
from gateway.config import Config, from_dict
from gateway.server import Gateway

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
CERTS = REPO_ROOT / "tests" / "fixtures" / "certs"

GATEWAY_AUTHORITY = "127.0.0.1:8080"


@pytest.fixture(scope="session")
def registry() -> ProfileRegistry:
    return ProfileRegistry.load_dir(EXAMPLES / "profiles")


@pytest.fixture()
def device():
    return load_device_adapter("http-plain")


@pytest.fixture()
def service():
    return load_service_adapter("origin-http")


def parse(raw: bytes, device, limits=None):
    """Parse ``raw`` through the device adapter (fails tests on surprises)."""
    return device.read_request(io.BytesIO(raw), limits or device.RequestLimits())


def request_bytes(
    method: str = "GET",
    target: str = "https://example.com/",
    version: str = "HTTP/1.0",
    headers: Optional[List[Tuple[str, str]]] = None,
    body: bytes = b"",
) -> bytes:
    lines = ["%s %s %s" % (method, target, version)]
    for name, value in headers or []:
        lines.append("%s: %s" % (name, value))
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    return head + body


class _OriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "FakeOrigin/1.0"
    sys_version = ""

    # Populated on the server class for assertions.
    last_headers: Dict[str, str] = {}

    def log_message(self, *args):  # keep test output quiet
        pass

    def _record(self) -> None:
        type(self).last_headers = {k.lower(): v for k, v in self.headers.items()}

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8",
              extra: Optional[List[Tuple[str, str]]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        self._record()
        path = self.path.split("?", 1)[0]
        if path == "/hello":
            page = (
                "<html><head><title>Hi</title><script>alert('x')</script></head>"
                '<body onclick="bad()">Hello \u2713 <a href="javascript:evil()">link</a>'
                "</body></html>"
            )
            self._send(200, page.encode("utf-8"))
        elif path == "/target":
            self._send(200, b"target-ok", content_type="text/plain; charset=utf-8")
        elif path == "/redirect":
            host, port = self.server.server_address[:2]
            location = "http://%s:%d/target" % (host, port)
            self._send(302, b"moved", extra=[("Location", location)])
        elif path == "/relative-redirect":
            self._send(302, b"moved", extra=[("Location", "/target")])
        elif path == "/big":
            size = int(self.headers.get("X-Fake-Size", "200000"))
            self._send(200, b"B" * size, content_type="text/plain; charset=utf-8")
        elif path == "/slow":
            time.sleep(1.5)
            self._send(200, b"slow-ok", content_type="text/plain; charset=utf-8")
        elif path == "/gzip":
            import gzip as gzip_mod

            payload = gzip_mod.compress(b"gzip-ok")
            self._send(200, payload, content_type="text/plain; charset=utf-8",
                       extra=[("Content-Encoding", "gzip")])
        elif path == "/br":
            self._send(200, b"br-bytes", content_type="text/plain; charset=utf-8",
                       extra=[("Content-Encoding", "br")])
        elif path == "/setcookie":
            self._send(200, b"cookie-page", content_type="text/plain; charset=utf-8",
                       extra=[("Set-Cookie", "session=supersecret123; Path=/"),
                              ("X-Internal-Header", "internal-value")])
        elif path == "/hugeheaders":
            self.send_response(200)
            for index in range(400):
                self.send_header("X-Pad-%03d" % index, "0123456789" * 10)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self._send(404, b"not found", content_type="text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        self._record()
        self._send(200, b"head-body", content_type="text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        self._record()
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        echoed = b"echo:" + body
        self._send(200, echoed, content_type=self.headers.get("Content-Type", "text/plain"))


class OriginServer:
    """Tiny local origin used as the mock upstream for integration tests."""

    def __init__(self, tls: bool = False, cafile: Optional[str] = None) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OriginHandler)
        self.httpd.daemon_threads = True
        self.tls = tls
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(
                certfile=str(CERTS / "cert.pem"), keyfile=str(CERTS / "key.pem")
            )
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "OriginServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def last_headers(self) -> Dict[str, str]:
        return dict(_OriginHandler.last_headers)

    def url(self, path: str, scheme: str = "http") -> str:
        return "%s://%s:%d%s" % (scheme, self.host, self.port, path)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def gateway_config(
    origin_port: int,
    allowed_schemes=("http",),
    deny_private_ranges: bool = False,
    upstream_timeout: float = 5.0,
    max_response_body: int = 1_048_576,
    **overrides,
) -> Config:
    document = {
        "listen": {"host": "127.0.0.1", "port": 0},
        "egress": {
            "allowed_schemes": list(allowed_schemes),
            "allowed_hosts": ["127.0.0.1", "localhost"],
            "allowed_ports": sorted({80, 443, origin_port}),
            "deny_private_ranges": deny_private_ranges,
        },
        "timeouts": {"client_read_s": 5.0, "client_write_s": 5.0, "upstream_s": upstream_timeout},
        "limits": {"max_response_body_bytes": max_response_body},
        "profiles_dir": str(EXAMPLES / "profiles"),
        "log_level": "debug",
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and key in document and isinstance(document[key], dict):
            document[key].update(value)
        else:
            document[key] = value
    return from_dict(document)


@pytest.fixture()
def running_gateway():
    """Factory: start a gateway on an ephemeral loopback port, stop it on teardown."""
    gateways: List[Tuple[Gateway, threading.Thread]] = []

    def _start(config: Config) -> Gateway:
        gateway = Gateway(config)
        gateway.create_server()
        thread = gateway.start_background()
        gateways.append((gateway, thread))
        return gateway

    yield _start

    for gateway, _thread in gateways:
        gateway.shutdown()


def gateway_port(gateway: Gateway) -> int:
    return int(gateway.server_address[1])


def read_response(sock: socket.socket, timeout: float = 5.0) -> bytes:
    sock.settimeout(timeout)
    chunks = []
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        chunks.append(chunk)
        data = b"".join(chunks)
        if b"\r\n\r\n" in data:
            head, _, body = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
            if len(body) >= length:
                break
    return b"".join(chunks)


def raw_request(port: int, payload: bytes, timeout: float = 5.0) -> bytes:
    """Send raw bytes to the gateway and read the whole (connection-close) reply."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(payload)
        return read_response(sock, timeout=timeout)


def split_response(payload: bytes) -> Tuple[bytes, bytes]:
    head, sep, body = payload.partition(b"\r\n\r\n")
    assert sep, "response is missing header terminator"
    return head, body


def parse_http_response(payload: bytes) -> Dict[str, object]:
    """Parse a raw device-facing response into {version, status, headers, body}."""
    head, body = split_response(payload)
    lines = head.decode("latin-1").split("\r\n")
    version, _, status_text = lines[0].partition(" ")
    status_code, _, reason = status_text.partition(" ")
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return {
        "version": version,
        "status": int(status_code),
        "reason": reason,
        "headers": headers,
        "body": body,
    }


class GatewayLogCapture:
    """Captures JSON log lines from the gateway logger for redaction asserts."""

    def __init__(self) -> None:
        self.stream = io.StringIO()
        import logging as _logging

        from core import logging as rb_logging

        self._handler = _logging.StreamHandler(self.stream)
        self._handler.setFormatter(rb_logging.JsonFormatter())
        self._logger = _logging.getLogger("retrobridge")
        self._logger.addHandler(self._handler)

    def __enter__(self) -> "GatewayLogCapture":
        return self

    def __exit__(self, *exc) -> None:
        self._logger.removeHandler(self._handler)

    @property
    def text(self) -> str:
        return self.stream.getvalue()

    def events(self) -> List[dict]:
        return [json.loads(line) for line in self.text.splitlines() if line.strip()]


def capture_gateway_log() -> GatewayLogCapture:
    return GatewayLogCapture()
