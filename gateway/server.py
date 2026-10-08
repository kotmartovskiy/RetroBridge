"""Gateway runtime: listener, session lifecycle, request handling (ARCHITECTURE §3.1–§4).

One thread per connection, one request per connection, ``Connection: close``
every time — deliberately conservative for legacy clients. All limits,
timeouts, egress policy and profile selection are enforced here in one
visible place; failures render as small device-legible errors with a trace id.
"""
from __future__ import annotations

import socket
import socketserver
import threading
import time
from typing import Dict, Optional

from core import logging as rb_logging
from core.errors import GatewayError, InternalError
from core.loader import load_device_adapter, load_service_adapter
from core.pipeline import Pipeline
from core.policy import check_egress, check_self_target
from core.profiles import ProfileRegistry, select_profile
from core.router import route as route_request
from core.session import SessionManager, peer_label
from gateway.config import Config

DEVICE_ADAPTER_NAME = "http-plain"
SERVICE_ADAPTER_NAME = "origin-http"


class Gateway:
    """Owns config, adapters, profiles, sessions and the request flow."""

    def __init__(
        self,
        config: Config,
        device_module=None,
        service_module=None,
        registry: Optional[ProfileRegistry] = None,
    ) -> None:
        self.config = config
        self.device = device_module if device_module is not None else load_device_adapter(DEVICE_ADAPTER_NAME)
        self.service = service_module if service_module is not None else load_service_adapter(SERVICE_ADAPTER_NAME)
        if registry is None:
            registry = ProfileRegistry.load_dir(config.profiles_dir)
        self.registry = registry
        self.sessions = SessionManager()
        self.logger = rb_logging.get_logger("gateway")
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._serving = threading.Event()
        self.bound_port: Optional[int] = None

    @property
    def actual_port(self) -> int:
        """The real listen port (resolves ephemeral port 0 after bind)."""
        return self.bound_port if self.bound_port else self.config.listen.port

    # -- request flow ------------------------------------------------------ #

    def handle_connection(self, conn: socket.socket, addr) -> None:
        session = self.sessions.open(peer_label(addr), self.device.TRANSPORT)
        version = "HTTP/1.0"
        method = "-"
        started = time.monotonic()
        status = 500
        reader = None
        writer = None
        try:
            try:
                conn.settimeout(self.config.timeouts.client_read_s)
                reader = conn.makefile("rb")
                writer = conn.makefile("wb")
            except OSError:
                return

            try:
                parsed = self.device.read_request(reader, self._request_limits())
                method = parsed.method
                version = parsed.version

                profile = select_profile(
                    self.registry, [self.device.probe(self.device.Observations.from_parsed(parsed))]
                )
                session.profile_id = profile.id
                session.adapter_id = self.device.ADAPTER_ID

                ir_request = self.device.to_ir(
                    parsed,
                    profile,
                    session.trace_id,
                    gateway_authority=self._gateway_authorities(),
                )
                check_self_target(
                    ir_request.target,
                    self.config.listen.host,
                    self.actual_port,
                    gateway_authority=self.config.gateway_authority,
                )
                decision = check_egress(ir_request.target, self.config.egress)
                route = route_request(ir_request)
                if not self.service.id().startswith(route.service_adapter_id):
                    raise GatewayError(detail="routed adapter is not loaded")
                ctx = self.service.AdapterContext(
                    trace_id=session.trace_id,
                    timeout_s=self.config.timeouts.upstream_s,
                    max_response_bytes=self.config.limits.max_response_body_bytes,
                    max_response_header_bytes=self.config.limits.max_response_header_bytes,
                    ca_file=self.config.egress.ca_file,
                )
                response = self.service.execute(
                    self.service.prepare(ir_request, decision, ctx), ctx
                )
                gateway_base = self._gateway_base(parsed, ir_request)
                pipeline = Pipeline.from_plan(route.plan)
                result = pipeline.run(
                    response, profile, request=ir_request, gateway_base=gateway_base
                )
                content_length = None
                if method == "HEAD":
                    upstream_length = result.meta.get("upstream_content_length")
                    if isinstance(upstream_length, int):
                        content_length = upstream_length
                payload = self.device.render_ir_response(
                    result, version=version, include_body=(method != "HEAD"),
                    content_length=content_length,
                )
                status = result.status
                self._write(writer, payload)
                self.logger.info(
                    "request",
                    extra={"fields": self._trace_fields(session, method, ir_request, status,
                                                         len(payload), started, profile.id)},
                )
            except GatewayError as exc:
                status = exc.status
                try:
                    payload = self.device.render_failure(exc, session.trace_id, version=version)
                    self._write(writer, payload)
                except GatewayError:
                    pass  # device vanished; nothing left to render to
                level = 30 if exc.status < 500 else 40  # warning / error
                self.logger.log(
                    level,
                    "request_failed",
                    extra={
                        "fields": {
                            "trace": session.trace_id,
                            "session": session.session_id,
                            "error": exc.code,
                            "status": exc.status,
                            "method": method,
                            "detail": exc.detail or "-",
                            "duration_ms": int((time.monotonic() - started) * 1000),
                        }
                    },
                )
            finally:
                for stream in (reader, writer):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
        except (ConnectionError, OSError):
            self.logger.debug(
                "connection_lost",
                extra={"fields": {"trace": session.trace_id, "session": session.session_id}},
            )
        except Exception:
            self.logger.error(
                "internal_error",
                exc_info=True,
                extra={"fields": {"trace": session.trace_id, "session": session.session_id}},
            )
            try:
                payload = self.device.render_failure(
                    InternalError(detail="unhandled"), session.trace_id, version=version
                )
                conn.sendall(payload)
            except OSError:
                pass
        finally:
            self.sessions.close(session, reason="status-%d" % status)
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    # -- helpers ----------------------------------------------------------- #

    def _write(self, writer, payload: bytes) -> None:
        try:
            writer.write(payload)
            writer.flush()
        except (ConnectionError, OSError) as exc:
            raise GatewayError(detail="write failed: %s" % exc) from exc

    def _request_limits(self):
        limits = self.config.limits
        return self.device.RequestLimits(
            max_request_line_bytes=limits.max_request_line_bytes,
            max_header_line_bytes=limits.max_header_line_bytes,
            max_header_count=limits.max_header_count,
            max_request_header_bytes=limits.max_request_header_bytes,
            max_body_bytes=limits.max_request_body_bytes,
        )

    def _gateway_authorities(self):
        """Authorities a device may use when addressing the gateway itself."""
        port = self.actual_port
        host = self.config.listen.host
        authorities = {"%s:%d" % (host, port)}
        if self.config.listen.is_wildcard or self.config.listen.is_loopback:
            for candidate in ("127.0.0.1", "localhost", "::1", "[::1]"):
                authorities.add("%s:%d" % (candidate, port))
        if port in (80, 443):
            authorities.add(host)
        return tuple(authorities)

    def _gateway_base(self, parsed, ir_request) -> str:
        """Base URL of the device-facing leg, for Location rewriting."""
        host_header = parsed.header_map.get("host", "").strip()
        if host_header and all(ord(char) < 128 and char not in "\r\n" for char in host_header):
            authority = host_header
        else:
            host = self.config.listen.host
            if self.config.listen.is_wildcard:
                host = "127.0.0.1"
            authority = "%s:%d" % (host, self.actual_port)
        return "http://%s" % authority

    def _trace_fields(self, session, method, ir_request, status, bytes_out, started, profile_id) -> Dict[str, object]:
        # Never log the query string or header values: they may carry tokens.
        return {
            "trace": session.trace_id,
            "session": session.session_id,
            "peer": session.peer,
            "method": method,
            "host": ir_request.target.host,
            "path": ir_request.target.path[:200],
            "scheme": ir_request.target.scheme,
            "profile": profile_id,
            "status": status,
            "bytes_out": bytes_out,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    # -- lifecycle --------------------------------------------------------- #

    def create_server(self) -> socketserver.ThreadingTCPServer:
        gateway = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                gateway.handle_connection(self.request, self.client_address)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Server((self.config.listen.host, self.config.listen.port), Handler)
        self._server = server
        self.bound_port = int(server.server_address[1])
        return server

    @property
    def server_address(self):
        return self._server.server_address if self._server else None

    def serve_forever(self) -> None:
        server = self._server or self.create_server()
        if not self.config.listen.is_loopback:
            self.logger.warning(
                "non_loopback_bind",
                extra={
                    "fields": {
                        "bind": "%s:%d" % (self.config.listen.host, self.config.listen.port),
                    }
                },
            )
        self.logger.info(
            "startup",
            extra={
                "fields": {
                    "bind": "%s:%s" % (server.server_address[0], server.server_address[1]),
                    "profiles": ",".join(self.registry.ids()),
                    "allowed_schemes": ",".join(self.config.egress.allowed_schemes),
                    "log_level": self.config.log_level,
                }
            },
        )
        self._serving.set()
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            self.logger.info(
                "shutdown",
                extra={"fields": {"open_sessions": self.sessions.open_count}},
            )

    def start_background(self) -> threading.Thread:
        if self._server is None:
            self.create_server()  # bind errors surface in the caller
        thread = threading.Thread(target=self.serve_forever, name="retrobridge-accept", daemon=True)
        thread.start()
        self._serving.wait(timeout=5.0)
        return thread

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        self._serving.clear()
