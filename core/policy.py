"""Egress, method and header policy (docs/ARCHITECTURE.md §6, PROTOCOL §6, §9).

Everything here is enforced *before* a request leaves the host or reaches a
device. SSRF defences are practical-by-default: scheme and port allowlists,
host allowlist with explicit wildcards, private/loopback/link-local address
blocking with DNS resolution performed *before* connect (the connection is
pinned to the checked IP while TLS SNI still uses the hostname), and a
self-target guard that stops the gateway from fetching itself.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import MethodNotAllowed, PolicyDenied
from .ir import Target

#: Schemes the gateway can fetch at all. Anything else is rejected at config
#: load *and* at request time, so a config typo cannot open an odd protocol.
SUPPORTED_SCHEMES = ("http", "https")

DEFAULT_ALLOWED_PORTS: Tuple[int, ...] = (80, 443)
DEFAULT_SCHEMES: Tuple[str, ...] = ("https",)

#: Device → upstream header allowlist (PROTOCOL §6). Everything else is stripped;
#: Host and Content-Length are recomputed by the service adapter.
REQUEST_HEADER_ALLOWLIST = frozenset(
    {
        "content-type",
        "content-length",
        "accept",
        "accept-language",
        "user-agent",
    }
)

#: Headers never forwarded upstream even if a device sends them.
REQUEST_HEADER_DENYLIST = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "authorization",
        "cookie",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-retrobridge-secret",
    }
)

#: Upstream → device response header allowlist (PROTOCOL §6).
RESPONSE_HEADER_ALLOWLIST = frozenset(
    {
        "content-type",
        "content-length",
        "location",
        "cache-control",
        "expires",
        "date",
        "last-modified",
        "retry-after",
        "etag",
    }
)

ALLOWED_METHODS = ("GET", "HEAD", "POST")

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")

Resolver = Callable[..., List[tuple]]


def normalize_scheme(scheme: str) -> str:
    return scheme.strip().lower()


def check_method(method: str) -> str:
    """Validate the device method against PROTOCOL §2.2 (GET/HEAD/POST)."""
    normalized = method.strip().upper()
    if normalized not in ALLOWED_METHODS:
        raise MethodNotAllowed("allowed methods: GET, HEAD, POST")
    return normalized


def host_matches(host: str, pattern: str) -> bool:
    """Exact host match, or ``*.suffix`` matching subdomains only (not apex)."""
    host = host.lower().rstrip(".")
    pattern = pattern.lower().rstrip(".")
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".example.com"
        return host.endswith(suffix) and host != suffix[1:]
    return host == pattern


def validate_allowed_hosts(patterns: Iterable[str]) -> Tuple[str, ...]:
    """Reject degenerate wildcards (``*``, ``*.``) at configuration time."""
    validated: List[str] = []
    for raw in patterns:
        pattern = str(raw).strip().lower()
        if not pattern:
            raise ValueError("empty host pattern in allowed_hosts")
        if pattern == "*" or pattern == "*." or pattern.startswith("*.") and pattern[2:] == "":
            raise ValueError("wildcard %r is too broad for allowed_hosts" % raw)
        if "*" in pattern and not pattern.startswith("*."):
            raise ValueError("host pattern %r may only use a leading '*.'" % raw)
        validated.append(pattern)
    return tuple(validated)


def validate_schemes(schemes: Iterable[str]) -> Tuple[str, ...]:
    normalized: List[str] = []
    for raw in schemes:
        scheme = normalize_scheme(str(raw))
        if scheme not in SUPPORTED_SCHEMES:
            raise ValueError(
                "unsupported outbound scheme %r (allowed: %s)"
                % (raw, ", ".join(SUPPORTED_SCHEMES))
            )
        if scheme not in normalized:
            normalized.append(scheme)
    if not normalized:
        raise ValueError("allowed_schemes must not be empty")
    return tuple(normalized)


@dataclass(frozen=True)
class EgressPolicy:
    """Declarative outbound policy for one service adapter (SERVICE_ADAPTERS §3)."""

    allowed_schemes: Tuple[str, ...] = DEFAULT_SCHEMES
    allowed_hosts: Tuple[str, ...] = ()
    allowed_ports: Tuple[int, ...] = DEFAULT_ALLOWED_PORTS
    deny_private_ranges: bool = True
    ca_file: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_schemes", validate_schemes(self.allowed_schemes))
        object.__setattr__(
            self, "allowed_hosts", validate_allowed_hosts(self.allowed_hosts)
        )
        ports = tuple(int(port) for port in self.allowed_ports)
        for port in ports:
            if not 1 <= port <= 65535:
                raise ValueError("invalid port in allowed_ports: %r" % port)
        object.__setattr__(self, "allowed_ports", ports)

    def allows_host(self, host: str) -> bool:
        return any(host_matches(host, pattern) for pattern in self.allowed_hosts)


@dataclass(frozen=True)
class EgressDecision:
    """Result of a successful policy check; carries the pinned connect IP."""

    scheme: str
    host: str
    port: int
    pinned_ip: str


def _is_forbidden_ip(ip: ipaddress._BaseAddress) -> bool:
    # ``is_global`` is False for private, loopback, link-local, reserved,
    # multicast and unspecified ranges — including cloud metadata (169.254.169.254).
    return not ip.is_global


def resolve_host(host: str, port: int, resolver: Optional[Resolver] = None) -> List[str]:
    """Resolve ``host`` to IP strings (raises PolicyDenied when unresolvable)."""
    resolve = resolver if resolver is not None else socket.getaddrinfo
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PolicyDenied("host did not resolve", detail="resolve_failed: %s" % exc) from exc
    addresses: List[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        address = str(sockaddr[0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise PolicyDenied("host did not resolve", detail="resolve_empty")
    return addresses


def _literal_ip(host: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def check_egress(
    target: Target,
    policy: EgressPolicy,
    resolver: Optional[Resolver] = None,
) -> EgressDecision:
    """Full outbound check for one target; raises :class:`PolicyDenied`.

    Order: scheme → port → host allowlist → DNS → address class. The
    "don't fetch yourself" rule lives in :func:`check_self_target` (host+port
    aware), so operators may deliberately allowlist loopback origins without
    the gateway being able to loop back into its own listener.
    """
    scheme = normalize_scheme(target.scheme)
    if scheme not in policy.allowed_schemes:
        raise PolicyDenied(
            "outbound scheme not allowed",
            detail="scheme=%r allowed=%r" % (scheme, policy.allowed_schemes),
        )
    if target.port not in policy.allowed_ports:
        raise PolicyDenied(
            "outbound port not allowed",
            detail="port=%r allowed=%r" % (target.port, policy.allowed_ports),
        )
    host = target.host.strip().lower().rstrip(".")
    if not host:
        raise PolicyDenied("empty host")
    if _literal_ip(host) is None and not _HOST_RE.match(host):
        raise PolicyDenied("host is not a valid hostname", detail="host=%r" % host)
    if not policy.allowed_hosts:
        raise PolicyDenied("no hosts are allowlisted", detail="allowed_hosts=()")
    if not policy.allows_host(host):
        raise PolicyDenied("host is not allowlisted", detail="host=%r" % host)

    addresses = resolve_host(host, target.port, resolver=resolver)
    if policy.deny_private_ranges:
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if _is_forbidden_ip(ip):
                raise PolicyDenied(
                    "destination address is not public",
                    detail="host=%r ip=%s" % (host, address),
                )
    return EgressDecision(
        scheme=scheme, host=host, port=target.port, pinned_ip=addresses[0]
    )


def check_self_target(
    target: Target, listen_host: str, listen_port: int, gateway_authority: Optional[str] = None
) -> None:
    """Reject targets that would make the gateway fetch itself (loop guard)."""
    candidates = {listen_host.lower()}
    if listen_host in ("0.0.0.0", "::", ""):
        candidates.update({"127.0.0.1", "localhost", "::1", "[::1]"})
    host = target.host.strip().lower().rstrip(".")
    if host in candidates and target.port == listen_port:
        raise PolicyDenied("refusing to fetch the gateway itself", detail="self_target")
    if gateway_authority:
        authority = gateway_authority.strip().lower()
        if host == authority:
            raise PolicyDenied("refusing to fetch the gateway itself", detail="self_target")
        name, _, port_text = authority.rpartition(":")
        if name and host == name.strip("[]") and port_text.isdigit() and int(port_text) == target.port:
            raise PolicyDenied("refusing to fetch the gateway itself", detail="self_target")


def sanitize_request_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    """Strip hop-by-hop, identity and device-supplied routing headers (§6)."""
    clean: Dict[str, str] = {}
    connection_tokens: List[str] = []
    connection = headers.get("connection", "")
    if connection:
        connection_tokens = [token.strip().lower() for token in connection.split(",") if token.strip()]
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in REQUEST_HEADER_DENYLIST:
            continue
        if lowered in connection_tokens:
            continue
        if lowered.startswith("proxy-") or lowered.startswith("x-forwarded-"):
            continue
        if lowered not in REQUEST_HEADER_ALLOWLIST:
            continue
        clean[lowered] = value
    return clean


def sanitize_response_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    """Keep only headers the device-facing surface is allowed to emit (§6)."""
    clean: Dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in RESPONSE_HEADER_ALLOWLIST:
            clean[lowered] = value
    return clean
