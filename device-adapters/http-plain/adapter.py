"""Device adapter: conservative plain HTTP/1.0-ish listener (Phase 1).

Speaks the smallest dialect legacy devices reliably manage: GET/HEAD/POST,
``HTTP/1.0`` and ``HTTP/1.1`` request lines, absolute-form or gateway
path-prefix targets, ``Content-Length`` bodies only (no chunked requests),
strict size limits and ``Connection: close`` in each response.

Contract: ``probe`` / ``parse_request_target`` / ``to_ir`` / ``render_*`` are
pure and unit-tested without sockets; only :func:`read_request` touches a
stream (any binary reader works, including ``io.BytesIO``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

from core.errors import (
    GatewayError,
    HeaderTooLarge,
    MalformedRequest,
    PolicyDenied,
    RateLimited,
    RequestTooLarge,
    render_error,
)
from core.ir import (
    MAX_HEADER_VALUE_SAMPLE,
    MAX_RAW_SAMPLE,
    IrRequest,
    IrResponse,
    OriginInfo,
    Target,
    Trace,
    truncate_sample,
)
from core.logging import is_sensitive
from core.policy import check_method, sanitize_request_headers, sanitize_response_headers
from core.profiles import CapabilityProfile, ProbeResult

ADAPTER_ID = "http-plain/1.0"
TRANSPORT = "http-plain"

_HEX = frozenset(b"0123456789abcdefABCDEF")
_TOKEN_RE = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_METHOD_RE = re.compile(rb"^[A-Za-z]+$")
_ABS_TARGET_RE = re.compile(rb"^([A-Za-z][A-Za-z0-9+.\-]*)://")
_PREFIX_RE = re.compile(rb"^/(https?)://", re.IGNORECASE)

_SAFE_PATH = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/?"
)
_SAFE_QUERY = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@/?"
)

_REASONS = {
    200: "OK",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    303: "See Other",
    304: "Not Modified",
    307: "Temporary Redirect",
    308: "Permanent Redirect",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    408: "Request Timeout",
    413: "Payload Too Large",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    502: "Bad Gateway",
    504: "Gateway Timeout",
}

#: Canonical spellings for the response header allowlist.
_CANONICAL = {
    "content-type": "Content-Type",
    "content-length": "Content-Length",
    "location": "Location",
    "cache-control": "Cache-Control",
    "expires": "Expires",
    "date": "Date",
    "last-modified": "Last-Modified",
    "retry-after": "Retry-After",
    "etag": "ETag",
    "connection": "Connection",
}

_TEXTUAL_ESSENCE_EXACT = (
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/x-www-form-urlencoded",
)


@dataclass(frozen=True)
class RequestLimits:
    """Listener-side hard caps; profile limits are enforced on top in ``to_ir``."""

    max_request_line_bytes: int = 2048
    max_header_line_bytes: int = 2048
    max_header_count: int = 64
    max_request_header_bytes: int = 8192
    max_body_bytes: int = 65536

    def __post_init__(self) -> None:
        for name in (
            "max_request_line_bytes",
            "max_header_line_bytes",
            "max_header_count",
            "max_request_header_bytes",
            "max_body_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError("%s must be > 0" % name)


@dataclass
class ParsedRequest:
    method: str
    target: bytes
    version: str
    headers: List[Tuple[str, str]] = field(default_factory=list)  # lowercased names
    body: bytes = b""
    header_bytes: int = 0

    @property
    def header_map(self) -> Dict[str, str]:
        merged: Dict[str, str] = {}
        for name, value in self.headers:
            if name in merged:
                merged[name] = ", ".join((merged[name], value))
            else:
                merged[name] = value
        return merged


@dataclass(frozen=True)
class Observations:
    version: str = "HTTP/1.0"
    user_agent: str = ""
    accept: str = ""
    method: str = "GET"

    @classmethod
    def from_parsed(cls, parsed: "ParsedRequest") -> "Observations":
        headers = parsed.header_map
        return cls(
            version=parsed.version,
            user_agent=headers.get("user-agent", ""),
            accept=headers.get("accept", ""),
            method=parsed.method,
        )


# --------------------------------------------------------------------------- #
# byte helpers
# --------------------------------------------------------------------------- #


def _is_valid_percent(raw: bytes, index: int) -> bool:
    return (
        index + 2 < len(raw)
        and raw[index + 1] in _HEX
        and raw[index + 2] in _HEX
    )


def percent_decode(raw: bytes) -> bytes:
    """Strict percent-decoding: malformed sequences are rejected, never guessed."""
    if b"%" not in raw:
        return raw
    out = bytearray()
    index = 0
    length = len(raw)
    while index < length:
        byte = raw[index]
        if byte == 0x25:  # '%'
            if not _is_valid_percent(raw, index):
                raise MalformedRequest("malformed percent-encoding in target")
            out.append(int(raw[index + 1 : index + 3], 16))
            index += 3
            continue
        out.append(byte)
        index += 1
    return bytes(out)


def _reencode(raw: bytes, safe: frozenset) -> bytes:
    """Canonicalize an already percent-encoded form to safe ASCII."""
    out = bytearray()
    index = 0
    length = len(raw)
    while index < length:
        byte = raw[index]
        if byte == 0x25 and _is_valid_percent(raw, index):
            out += raw[index : index + 3]
            index += 3
            continue
        if byte in safe:
            out.append(byte)
        else:
            out += b"%%%02X" % byte
        index += 1
    return bytes(out)


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _split_authority(authority: bytes, scheme: str) -> Tuple[str, int]:
    if b"@" in authority:
        raise PolicyDenied("userinfo in request target is not allowed", detail="userinfo")
    host = authority
    port_text = b""
    if authority.startswith(b"["):
        end = authority.find(b"]")
        if end < 0:
            raise MalformedRequest("unterminated IPv6 literal in target")
        host = authority[: end + 1]
        rest = authority[end + 1 :]
        if rest.startswith(b":"):
            port_text = rest[1:]
        elif rest:
            raise MalformedRequest("malformed authority in target")
        host_display = host.decode("ascii", errors="replace").strip("[]")
    else:
        if authority.count(b":") > 1:
            raise MalformedRequest("malformed authority in target")
        if b":" in authority:
            host, _, port_text = authority.partition(b":")
        host_display = host.decode("ascii", errors="replace")
    if not host_display:
        raise MalformedRequest("empty host in request target")
    if port_text:
        if not port_text.isdigit():
            raise MalformedRequest("malformed port in target")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise MalformedRequest("port out of range in target")
    else:
        port = _default_port(scheme)
    if "%" in host_display:
        raise MalformedRequest("percent-encoded host in target")
    return host_display, port


def _parse_absolute(raw: bytes, charset: str) -> Target:
    match = _ABS_TARGET_RE.match(raw)
    if not match:
        raise MalformedRequest("request target is not absolute-form")
    scheme = match.group(1).decode("ascii", errors="replace").lower()
    rest = raw[match.end() :].split(b"#", 1)[0]
    cut = len(rest)
    for delimiter in (b"/", b"?"):
        position = rest.find(delimiter)
        if position >= 0:
            cut = min(cut, position)
    authority = rest[:cut]
    remainder = rest[cut:]
    if not authority:
        raise MalformedRequest("missing host in request target")
    host, port = _split_authority(authority, scheme)

    path_part, _, query_part = remainder.partition(b"?")
    if not path_part:
        path_part = b"/"
    if not path_part.startswith(b"/"):
        raise MalformedRequest("path in target must start with '/'")

    decoded_path = percent_decode(path_part)
    decoded_query = percent_decode(query_part)
    return Target(
        scheme=scheme,
        host=host,
        port=port,
        path=decoded_path.decode(charset, errors="replace"),
        query=decoded_query.decode(charset, errors="replace"),
        raw_path=_reencode(path_part, _SAFE_PATH).decode("ascii"),
        raw_query=_reencode(query_part, _SAFE_QUERY).decode("ascii"),
    )


def _authority_set(gateway_authority) -> set:
    """Normalize gateway authority input (str or iterable of str) to bytes."""
    if not gateway_authority:
        return set()
    if isinstance(gateway_authority, str):
        items = [gateway_authority]
    else:
        items = [item for item in gateway_authority if item]
    return {
        str(item).strip().lower().encode("ascii", errors="replace") for item in items
    }


def parse_request_target(
    raw: bytes, charset: str, gateway_authority=None
) -> Target:
    """Normalize absolute-form and gateway path-prefix targets to one shape.

    Accepted forms (PROTOCOL §2.2):

    * ``https://host/path?query``   — proxy-style absolute target
    * ``/https://host/path?query``  — gateway path-prefix target
    * ``http://<gateway>/<scheme>://host/...`` — absolute target aimed at the
      gateway itself; unwrapped when the path carries a scheme prefix
    ``gateway_authority`` may be one authority (``"host:port"``) or several.
    """
    if not raw or raw.isspace():
        raise MalformedRequest("empty request target")
    raw = raw.strip()

    absolute = _ABS_TARGET_RE.match(raw)
    if absolute:
        rest = raw[absolute.end() :]
        cut = len(rest)
        for delimiter in (b"/", b"?", b"#"):
            position = rest.find(delimiter)
            if position >= 0:
                cut = min(cut, position)
        authority = rest[:cut]
        gateway_authorities = _authority_set(gateway_authority)
        if gateway_authorities and authority.lower() in gateway_authorities:
            path = rest[cut:].split(b"#", 1)[0]
            if _PREFIX_RE.match(path):
                return parse_request_target(path[1:], charset, gateway_authority=None)
            raise MalformedRequest(
                "gateway target must use /<scheme>://<host>/ path-prefix"
            )
        return _parse_absolute(raw, charset)

    if raw.startswith(b"/"):
        if _PREFIX_RE.match(raw):
            return parse_request_target(raw[1:], charset, gateway_authority=None)
        if raw.startswith(b"//"):
            return parse_request_target(b"http:" + raw, charset, gateway_authority=None)
        raise MalformedRequest(
            "origin-form target requires /<scheme>://<host>/ path-prefix"
        )

    raise MalformedRequest("unsupported request target form")


def _parse_content_type(value: str) -> Tuple[Optional[str], Optional[str]]:
    parts = [part.strip() for part in value.split(";")]
    media = parts[0].lower() or None
    charset: Optional[str] = None
    for parameter in parts[1:]:
        name, _, raw_value = parameter.partition("=")
        if name.strip().lower() == "charset":
            charset = raw_value.strip().strip('"').strip("'") or None
    return media, charset


# --------------------------------------------------------------------------- #
# stream parsing
# --------------------------------------------------------------------------- #


def _read_line(stream, limit: int, what: str) -> bytes:
    line = stream.readline(limit + 1)
    if len(line) > limit:
        raise HeaderTooLarge("%s exceeds limit" % what)
    return line


def _has_control_chars(value: str) -> bool:
    return any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value)


def read_request(stream, limits: RequestLimits) -> ParsedRequest:
    """Parse one request off ``stream``; every failure maps to a 4xx class."""
    request_line = _read_line(stream, limits.max_request_line_bytes, "request line")
    if not request_line:
        raise MalformedRequest("empty request")
    request_line = request_line.rstrip(b"\r\n")
    if not request_line.strip():
        raise MalformedRequest("empty request line")
    parts = request_line.split()
    if len(parts) != 3:
        raise MalformedRequest("request line must be: METHOD TARGET VERSION")
    method_raw, target, version_raw = parts
    if not _METHOD_RE.match(method_raw):
        raise MalformedRequest("invalid method token")
    method = check_method(method_raw.decode("ascii"))  # 405 for non GET/HEAD/POST
    version = version_raw.decode("ascii", errors="replace")
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise MalformedRequest("unsupported HTTP version")

    headers: List[Tuple[str, str]] = []
    header_bytes = len(request_line) + 2
    previous_index: Optional[int] = None
    while True:
        line = _read_line(stream, limits.max_header_line_bytes, "header line")
        if not line:
            raise MalformedRequest("unexpected end of stream in headers")
        header_bytes += len(line)
        if header_bytes > limits.max_request_header_bytes:
            raise HeaderTooLarge("total header bytes exceed limit")
        stripped = line.rstrip(b"\r\n")
        if not stripped:
            break
        if stripped[:1] in (b" ", b"\t"):  # obs-fold continuation (tolerated)
            if previous_index is None or not headers:
                raise MalformedRequest("orphaned header continuation line")
            folded = stripped.strip().decode("latin-1")
            if _has_control_chars(folded):
                raise MalformedRequest("control character in header value")
            name, value = headers[previous_index]
            headers[previous_index] = (name, (value + " " + folded))
            continue
        if len(headers) >= limits.max_header_count:
            raise HeaderTooLarge("too many header fields")
        if b":" not in stripped:
            raise MalformedRequest("header line without ':'")
        name_raw, _, value_raw = stripped.partition(b":")
        name_stripped = name_raw.strip(b" \t")
        if not _TOKEN_RE.match(name_stripped):
            raise MalformedRequest("invalid header field name")
        value = value_raw.strip(b" \t").decode("latin-1")
        if _has_control_chars(value):
            raise MalformedRequest("control character in header value")
        headers.append((name_stripped.decode("ascii").lower(), value))
        previous_index = len(headers) - 1

    header_map: Dict[str, str] = {}
    for name, value in headers:
        if name == "content-length" and name in header_map and header_map[name] != value:
            raise MalformedRequest("conflicting Content-Length headers")
        header_map[name] = value

    if "transfer-encoding" in header_map:
        raise MalformedRequest("chunked/transfer-encoded requests are not supported")

    body = b""
    content_length = header_map.get("content-length")
    if content_length is not None:
        if not content_length.isdigit():
            raise MalformedRequest("invalid Content-Length")
        declared = int(content_length)
        if declared > limits.max_body_bytes:
            raise RequestTooLarge("request body exceeds limit")
        remaining = declared
        chunks: List[bytes] = []
        while remaining > 0:
            chunk = stream.read(min(remaining, 65536))
            if not chunk:
                raise MalformedRequest("request body shorter than Content-Length")
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)

    return ParsedRequest(
        method=method,
        target=target,
        version=version,
        headers=headers,
        body=body,
        header_bytes=header_bytes,
    )


# --------------------------------------------------------------------------- #
# probe / IR
# --------------------------------------------------------------------------- #


def probe(observations: Observations) -> ProbeResult:
    """Conservative profile suggestion from request-line + User-Agent evidence."""
    user_agent = (observations.user_agent or "").lower()
    if "midp" in user_agent or "cldc" in user_agent or "j2me" in user_agent:
        return ProbeResult(score=50, profile_id="j2me-midp2-generic")
    if observations.version in ("HTTP/1.0", "HTTP/1.1"):
        return ProbeResult(score=10, profile_id="generic-constrained")
    return ProbeResult.no_match()


def to_ir(
    parsed: ParsedRequest,
    profile: CapabilityProfile,
    trace_id: str,
    gateway_authority=None,
    seq: int = 1,
) -> IrRequest:
    """Build the normalized IR; enforces the *profile's* limits on top of the
    listener's hard caps (PROTOCOL §2.2, §3.1)."""
    if parsed.header_bytes > profile.transport.max_request_header_bytes:
        raise HeaderTooLarge("headers exceed profile limit")
    if len(parsed.body) > profile.content.max_body_bytes:
        raise RequestTooLarge("body exceeds profile limit")

    headers = parsed.header_map
    media: Optional[str] = None
    charset: Optional[str] = None
    if "content-type" in headers:
        media, charset = _parse_content_type(headers["content-type"])
    target = parse_request_target(
        parsed.target, profile.default_charset, gateway_authority
    )

    safe_headers = sanitize_request_headers(headers)
    raw_headers = [
        (
            name,
            "<redacted>"
            if is_sensitive(name)
            else truncate_sample(value, MAX_HEADER_VALUE_SAMPLE),
        )
        for name, value in parsed.headers
    ]
    return IrRequest(
        method=parsed.method,
        target=target,
        headers=safe_headers,
        body=parsed.body,
        content_type=media,
        charset=charset,
        origin=OriginInfo(
            adapter_id=ADAPTER_ID,
            raw_target=truncate_sample(parsed.target.decode("latin-1"), MAX_RAW_SAMPLE),
            raw_headers=raw_headers,
            transport=TRANSPORT,
            profile_id=profile.id,
        ),
        trace=Trace(trace_id=trace_id, seq=seq),
    )


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _charset_applies(content_type: str) -> bool:
    essence = content_type.split(";", 1)[0].strip().lower()
    return essence.startswith("text/") or essence in _TEXTUAL_ESSENCE_EXACT


def _safe_header_value(value: str) -> str:
    """Neutralize response-splitting: no CR, LF or NUL ever reaches the wire."""
    return str(value).replace("\r", " ").replace("\n", " ").replace("\x00", "")


def render_response(
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    version: str = "HTTP/1.0",
    include_body: bool = True,
    content_length: Optional[int] = None,
) -> bytes:
    """Serialize one device-facing response with ``Connection: close``.

    ``content_length`` overrides the computed length (HEAD responses mirror the
    origin's declared length for the equivalent GET).
    """
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        version = "HTTP/1.0"
    reason = _REASONS.get(status, "Status")
    lines = ["%s %d %s" % (version, status, reason)]

    clean = sanitize_response_headers(dict(headers))
    clean.pop("connection", None)
    clean.pop("transfer-encoding", None)
    for name in list(clean):
        clean[name] = _safe_header_value(clean[name])

    body = body or b""
    no_body_status = status in (204, 304)
    send_body = include_body and not no_body_status

    if not no_body_status and body and not clean.get("content-type"):
        clean["content-type"] = "text/plain; charset=us-ascii"
    content_type = clean.get("content-type")
    if content_type and _charset_applies(content_type) and "charset=" not in content_type.lower():
        clean["content-type"] = content_type + "; charset=us-ascii"
    if not no_body_status:
        if content_length is None:
            content_length = len(body)
        clean["content-length"] = str(max(0, int(content_length)))
    clean["connection"] = "close"

    emitted = set()
    for name in (
        "content-type",
        "content-length",
        "location",
        "cache-control",
        "date",
        "last-modified",
        "retry-after",
        "expires",
        "etag",
        "connection",
    ):
        if name in clean and name not in emitted:
            lines.append("%s: %s" % (_CANONICAL[name], clean[name]))
            emitted.add(name)
    for name in sorted(clean):
        if name in emitted:
            continue
        lines.append("%s: %s" % (_CANONICAL.get(name, name.title()), clean[name]))

    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", errors="replace")
    if send_body:
        return head + body
    return head


def render_failure(
    exc: GatewayError, trace_id: str, version: str = "HTTP/1.0", include_body: bool = True
) -> bytes:
    """Map any internal error to its device-legible response (PROTOCOL §7)."""
    body = render_error(exc, trace_id)
    headers: Dict[str, str] = {"content-type": "text/plain; charset=us-ascii"}
    if isinstance(exc, RateLimited):
        headers["retry-after"] = "5"
    return render_response(
        exc.status, headers, body, version=version, include_body=include_body
    )


def render_ir_response(
    response: IrResponse,
    version: str = "HTTP/1.0",
    include_body: bool = True,
    content_length: Optional[int] = None,
) -> bytes:
    """Render a post-pipeline IR response for the device."""
    return render_response(
        response.status,
        response.headers,
        response.body,
        version=version,
        include_body=include_body,
        content_length=content_length,
    )


def id() -> str:
    return ADAPTER_ID


def transports() -> Tuple[str, ...]:
    return (TRANSPORT,)


def close() -> None:
    """Phase 1 adapter holds no pooled state."""
    return None
