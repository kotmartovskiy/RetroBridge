"""Service adapter: minimal origin fetch over modern TLS (Phase 1).

Contract (docs/SERVICE_ADAPTERS.md §2): ``prepare`` is pure (URL/headers/body),
``execute`` is the only I/O point. The connection is pinned to the IP that
passed egress policy while TLS SNI/verification still uses the hostname, so a
hostile DNS answer cannot retarget the fetch after the check (practical SSRF
hardening). Redirects are never followed here — the gateway hands them back to
the device as gateway URLs and policy is re-checked on every hop.
"""
from __future__ import annotations

import gzip
import http.client
import io
import socket
import ssl
import time
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.errors import PolicyDenied, UpstreamFailure, UpstreamTimeout
from core.ir import IrRequest, IrResponse
from core.policy import EgressPolicy, normalize_scheme, sanitize_response_headers

ADAPTER_ID = "origin-http/1.0"
GATEWAY_USER_AGENT = "RetroBridge/0.1.0"

#: Declarative defaults; mirrored by ``policy.json`` (kept in sync by tests).
DEFAULT_EGRESS = {
    "allowed_schemes": ["https"],
    "allowed_hosts": [],
    "allowed_ports": [80, 443],
    "deny_private_ranges": True,
}

SUPPORTED_SCHEMES = ("http", "https")
_COMPRESSIBLE_ENCODINGS = ("gzip", "x-gzip", "deflate")


@dataclass(frozen=True)
class AdapterContext:
    trace_id: str
    timeout_s: float = 10.0
    max_response_bytes: int = 1_048_576
    max_response_header_bytes: int = 16_384
    ca_file: Optional[str] = None
    user_agent: str = GATEWAY_USER_AGENT


@dataclass(frozen=True)
class OutboundRequest:
    method: str
    scheme: str
    host: str
    port: int
    target: str  # origin-form: /path?query
    headers: Tuple[Tuple[str, str], ...]
    body: bytes
    timeout_s: float
    max_response_bytes: int
    max_response_header_bytes: int
    pinned_ip: str
    ca_file: Optional[str] = None


def id() -> str:
    return ADAPTER_ID


def matches(ir_request: IrRequest, policy: EgressPolicy) -> bool:
    """Pure allowlist check (no DNS): does this adapter own the target?"""
    target = ir_request.target
    scheme = normalize_scheme(target.scheme)
    return (
        scheme in policy.allowed_schemes
        and policy.allows_host(target.host.lower())
        and target.port in policy.allowed_ports
    )


def prepare(
    ir_request: IrRequest, decision, ctx: AdapterContext
) -> OutboundRequest:
    """Pure request shaping: origin-form target + minimal, sanitized headers."""
    target = ir_request.target
    scheme = normalize_scheme(decision.scheme or target.scheme)
    if scheme not in SUPPORTED_SCHEMES:
        raise PolicyDenied("unsupported outbound scheme", detail="scheme=%r" % scheme)

    default_port = 443 if scheme == "https" else 80
    host_header = (
        target.host if decision.port == default_port else "%s:%d" % (target.host, decision.port)
    )

    headers: List[Tuple[str, str]] = [
        ("Host", host_header),
        ("User-Agent", ir_request.headers.get("user-agent") or ctx.user_agent),
        ("Accept", ir_request.headers.get("accept") or "*/*"),
        ("Accept-Encoding", "identity"),
        ("Connection", "close"),
    ]
    for name in ("content-type", "accept-language"):
        value = ir_request.headers.get(name)
        if value:
            headers.append((name.title(), value))
    if ir_request.body and ir_request.method in ("POST",):
        headers.append(("Content-Length", str(len(ir_request.body))))

    return OutboundRequest(
        method=ir_request.method,
        scheme=scheme,
        host=target.host,
        port=decision.port,
        target=target.raw_origin_form,
        headers=tuple(headers),
        body=ir_request.body if ir_request.method == "POST" else b"",
        timeout_s=ctx.timeout_s,
        max_response_bytes=ctx.max_response_bytes,
        max_response_header_bytes=ctx.max_response_header_bytes,
        pinned_ip=decision.pinned_ip,
        ca_file=ctx.ca_file,
    )


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that dials the pre-validated IP, not a fresh DNS answer."""

    def __init__(self, host: str, port: Optional[int] = None, **kwargs):
        self._pinned_ip = kwargs.pop("pinned_ip", None) or host
        super().__init__(host, port, **kwargs)

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to the checked IP with hostname verification."""

    def __init__(self, host: str, port: Optional[int] = None, **kwargs):
        context = kwargs.pop("ssl_context", None)
        self._pinned_ip = kwargs.pop("pinned_ip", None) or host
        super().__init__(host, port, context=context, **kwargs)

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        context = self._context if self._context is not None else ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self.host)


def _tls_context(ca_file: Optional[str]) -> ssl.SSLContext:
    """Current, default-strength upstream TLS (never weakened for a device)."""
    return ssl.create_default_context(cafile=ca_file)


def _decode_bounded(data: bytes, encoding: str, limit: int) -> Tuple[bytes, bool]:
    """Decompress within ``limit``; returns (bytes, truncated)."""
    try:
        if encoding in ("gzip", "x-gzip"):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
                out = handle.read(limit + 1)
        else:  # deflate
            decompressor = zlib.decompressobj()
            out = decompressor.decompress(data, limit + 1)
    except (OSError, EOFError, zlib.error, ValueError) as exc:
        raise UpstreamFailure("upstream sent undecodable content-encoding",
                              detail="decode=%s: %s" % (encoding, exc)) from exc
    if len(out) > limit:
        return out[:limit], True
    return out, False


def execute(outbound: OutboundRequest, ctx: AdapterContext) -> IrResponse:
    """The only I/O point of this adapter: one bounded, pinned fetch."""
    if outbound.scheme not in SUPPORTED_SCHEMES:
        raise PolicyDenied(
            "unsupported outbound scheme", detail="scheme=%r" % outbound.scheme
        )

    started = time.time()
    connection = None
    try:
        if outbound.scheme == "https":
            connection = _PinnedHTTPSConnection(
                outbound.host,
                outbound.port,
                timeout=outbound.timeout_s,
                ssl_context=_tls_context(outbound.ca_file),
                pinned_ip=outbound.pinned_ip,
            )
        else:
            connection = _PinnedHTTPConnection(
                outbound.host,
                outbound.port,
                timeout=outbound.timeout_s,
                pinned_ip=outbound.pinned_ip,
            )
        headers = dict(outbound.headers)
        body = outbound.body if outbound.method in ("POST", "GET", "HEAD") else b""
        connection.request(outbound.method, outbound.target, body=body or None, headers=headers)
        response = connection.getresponse()

        header_size = sum(len(name) + len(value) + 4 for name, value in response.getheaders())
        if header_size > outbound.max_response_header_bytes:
            raise UpstreamFailure(
                "upstream response headers too large",
                detail="header_bytes=%d" % header_size,
            )

        status = response.status
        raw_headers: Dict[str, str] = {}
        for name, value in response.getheaders():
            lowered = name.lower()
            if lowered in raw_headers:
                raw_headers[lowered] = ", ".join((raw_headers[lowered], value))
            else:
                raw_headers[lowered] = value

        limit = outbound.max_response_bytes
        chunks: List[bytes] = []
        total = 0
        truncated = False
        while True:
            chunk = response.read(min(65536, limit - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                truncated = True
                break
        data = b"".join(chunks)
        if truncated:
            data = data[:limit]

        encoding = raw_headers.get("content-encoding", "").lower().strip()
        if encoding and encoding != "identity":
            if encoding in _COMPRESSIBLE_ENCODINGS:
                data, decoded_truncated = _decode_bounded(data, encoding, limit)
                truncated = truncated or decoded_truncated
            else:
                raise UpstreamFailure(
                    "unsupported upstream content-encoding",
                    detail="encoding=%r" % encoding[:32],
                )
        raw_headers.pop("content-encoding", None)

        headers_out = sanitize_response_headers(raw_headers)
        headers_out.pop("content-length", None)
        meta = {
            "adapter_id": ADAPTER_ID,
            "fetched_at": started,
            "cache": "bypass",
            "upstream_host": outbound.host,
            "bytes_in": len(data),
            "upstream_status": status,
        }
        upstream_length = raw_headers.get("content-length", "")
        if upstream_length.isdigit():
            meta["upstream_content_length"] = int(upstream_length)
        if truncated:
            meta["upstream_truncated"] = True
        return IrResponse(status=status, headers=headers_out, body=data, meta=meta)
    except (PolicyDenied, UpstreamFailure, UpstreamTimeout):
        raise
    except socket.timeout as exc:
        raise UpstreamTimeout("upstream did not respond", detail="timeout") from exc
    except ssl.SSLError as exc:
        raise UpstreamFailure("upstream TLS failure", detail="ssl: %s" % exc) from exc
    except (http.client.HTTPException, OSError) as exc:
        raise UpstreamFailure(
            "upstream request failed", detail="%s: %s" % (type(exc).__name__, exc)
        ) from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # pragma: no cover - defensive
                pass


def fetch(
    ir_request: IrRequest, decision, ctx: AdapterContext
) -> IrResponse:
    """Convenience: ``prepare`` + ``execute`` (used by the gateway runtime)."""
    return execute(prepare(ir_request, decision, ctx), ctx)


def authenticate(ctx: AdapterContext) -> str:
    """Phase 1 origin fetch needs no credentials."""
    return "ready"


def close() -> None:
    """No pools or timers are held in Phase 1."""
    return None
