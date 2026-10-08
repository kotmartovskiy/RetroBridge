"""Egress policy, SSRF guards and header policy (ARCHITECTURE §6, PROTOCOL §6)."""
from __future__ import annotations

import socket

import pytest

from core.errors import MethodNotAllowed, PolicyDenied
from core.ir import Target
from core.policy import (
    EgressPolicy,
    check_egress,
    check_method,
    check_self_target,
    host_matches,
    sanitize_request_headers,
    sanitize_response_headers,
    validate_allowed_hosts,
    validate_schemes,
)


def make_target(scheme="https", host="example.com", port=443, path="/x", query=""):
    raw_path = path
    return Target(scheme=scheme, host=host, port=port, path=path, query=query,
                  raw_path=raw_path, raw_query=query)


def resolver_returning(*addresses):
    def resolver(host, port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    return resolver


def resolver_failing(host, port, **_kwargs):
    raise socket.gaierror("no such host")


# -- methods ----------------------------------------------------------------- #


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "get", "Post"])
def test_allowed_methods(method):
    assert check_method(method) in ("GET", "HEAD", "POST")


@pytest.mark.parametrize("method", ["DELETE", "PUT", "OPTIONS", "CONNECT"])
def test_denied_methods(method):
    with pytest.raises(MethodNotAllowed):
        check_method(method)


# -- schemes, hosts, ports --------------------------------------------------- #


def test_default_policy_allows_only_https():
    policy = EgressPolicy(allowed_hosts=("example.com",))
    assert policy.allowed_schemes == ("https",)
    decision = check_egress(make_target(scheme="https"), policy,
                            resolver=resolver_returning("93.184.216.34"))
    assert decision.pinned_ip == "93.184.216.34"


def test_http_denied_by_default():
    policy = EgressPolicy(allowed_hosts=("example.com",))
    with pytest.raises(PolicyDenied):
        check_egress(make_target(scheme="http", port=80), policy,
                     resolver=resolver_returning("93.184.216.34"))


def test_unsupported_scheme_rejected_at_construction():
    with pytest.raises(ValueError):
        EgressPolicy(allowed_schemes=("ftp",))
    with pytest.raises(ValueError):
        validate_schemes(["file", "gopher"])


def test_empty_schemes_rejected():
    with pytest.raises(ValueError):
        EgressPolicy(allowed_schemes=())


def test_host_not_allowlisted():
    policy = EgressPolicy(allowed_hosts=("example.com",))
    with pytest.raises(PolicyDenied) as excinfo:
        check_egress(make_target(host="evil.test"), policy,
                     resolver=resolver_returning("93.184.216.34"))
    assert "allowlist" in (excinfo.value.detail or "") or "allowlist" in excinfo.value.text


def test_no_allowlist_denies_everything():
    policy = EgressPolicy()  # empty allowlist
    with pytest.raises(PolicyDenied):
        check_egress(make_target(), policy, resolver=resolver_returning("93.184.216.34"))


@pytest.mark.parametrize("host,pattern,expected", [
    ("example.com", "example.com", True),
    ("www.example.com", "*.example.com", True),
    ("a.b.example.com", "*.example.com", True),
    ("example.com", "*.example.com", False),
    ("notexample.com", "example.com", False),
    ("EXAMPLE.com", "example.com", True),
    ("example.com.", "example.com", True),
])
def test_host_matching(host, pattern, expected):
    assert host_matches(host, pattern) is expected


def test_wildcard_policy_matching():
    policy = EgressPolicy(allowed_hosts=("*.example.com",))
    assert policy.allows_host("www.example.com")
    assert not policy.allows_host("example.com")
    assert not policy.allows_host("example.net")


def test_broad_wildcards_rejected_at_config_time():
    with pytest.raises(ValueError):
        validate_allowed_hosts(["*"])
    with pytest.raises(ValueError):
        validate_allowed_hosts(["foo.*"])
    with pytest.raises(ValueError):
        validate_allowed_hosts([""])


def test_port_not_allowed():
    policy = EgressPolicy(allowed_hosts=("example.com",), allowed_ports=(443,))
    with pytest.raises(PolicyDenied):
        check_egress(make_target(port=8443), policy,
                     resolver=resolver_returning("93.184.216.34"))


def test_invalid_port_in_policy_rejected():
    with pytest.raises(ValueError):
        EgressPolicy(allowed_ports=(0,))
    with pytest.raises(ValueError):
        EgressPolicy(allowed_ports=(70000,))


# -- SSRF: private ranges ---------------------------------------------------- #


PRIVATE_ADDRESSES = ("10.0.0.5", "192.168.1.1", "172.16.0.1", "127.0.0.1",
                     "169.254.169.254", "0.0.0.0", "::1", "fe80::1")


@pytest.mark.parametrize("address", PRIVATE_ADDRESSES)
def test_private_ranges_denied(address):
    policy = EgressPolicy(allowed_hosts=("example.com",), deny_private_ranges=True)
    with pytest.raises(PolicyDenied):
        check_egress(make_target(), policy, resolver=resolver_returning(address))


def test_public_range_allowed_with_deny_enabled():
    policy = EgressPolicy(allowed_hosts=("example.com",), deny_private_ranges=True)
    decision = check_egress(make_target(), policy,
                            resolver=resolver_returning("93.184.216.34"))
    assert decision.pinned_ip == "93.184.216.34"


def test_private_range_allowed_when_explicitly_disabled():
    policy = EgressPolicy(allowed_hosts=("127.0.0.1",), deny_private_ranges=False,
                          allowed_ports=(8080,))
    decision = check_egress(make_target(host="127.0.0.1", port=8080), policy,
                            resolver=resolver_returning("127.0.0.1"))
    assert decision.pinned_ip == "127.0.0.1"


def test_all_resolved_addresses_checked():
    policy = EgressPolicy(allowed_hosts=("example.com",))
    with pytest.raises(PolicyDenied):
        check_egress(
            make_target(), policy,
            resolver=resolver_returning("93.184.216.34", "10.0.0.1"),
        )


def test_resolution_failure_denied():
    policy = EgressPolicy(allowed_hosts=("example.com",))
    with pytest.raises(PolicyDenied):
        check_egress(make_target(), policy, resolver=resolver_failing)


def test_hostname_with_bad_characters_denied():
    policy = EgressPolicy(allowed_hosts=("exa mple.com",))
    with pytest.raises(PolicyDenied):
        check_egress(make_target(host="exa mple.com"), policy,
                     resolver=resolver_returning("93.184.216.34"))


# -- self-target guard ------------------------------------------------------- #


def test_self_target_by_listen_address():
    with pytest.raises(PolicyDenied):
        check_self_target(make_target(host="127.0.0.1", port=8080), "127.0.0.1", 8080)


def test_self_target_wildcard_bind_protects_loopback():
    with pytest.raises(PolicyDenied):
        check_self_target(make_target(host="127.0.0.1", port=8080), "0.0.0.0", 8080)
    with pytest.raises(PolicyDenied):
        check_self_target(make_target(host="localhost", port=8080), "0.0.0.0", 8080)


def test_self_target_ignores_other_ports():
    check_self_target(make_target(host="127.0.0.1", port=9090), "127.0.0.1", 8080)


def test_self_target_via_gateway_authority():
    with pytest.raises(PolicyDenied):
        check_self_target(make_target(host="gateway.lan", port=9090),
                          "127.0.0.1", 8080, gateway_authority="gateway.lan:9090")


# -- header policy ----------------------------------------------------------- #


def test_request_headers_stripped():
    clean = sanitize_request_headers({
        "host": "example.com",
        "connection": "keep-alive, X-Custom-Hop",
        "keep-alive": "timeout=5",
        "proxy-authorization": "Basic abc",
        "transfer-encoding": "chunked",
        "authorization": "Bearer secret",
        "cookie": "sid=1",
        "x-forwarded-for": "10.0.0.1",
        "x-custom-hop": "yes",
        "content-type": "text/plain",
        "accept": "*/*",
        "user-agent": "OldPhone/1.0",
        "x-retrobridge-secret": "nope",
    })
    assert clean == {
        "content-type": "text/plain",
        "accept": "*/*",
        "user-agent": "OldPhone/1.0",
    }


def test_response_headers_allowlisted():
    clean = sanitize_response_headers({
        "content-type": "text/html",
        "location": "/next",
        "set-cookie": "session=secret",
        "set-cookie2": "x",
        "transfer-encoding": "chunked",
        "content-security-policy": "default-src none",
        "server": "InternalServer/9.9",
        "x-internal": "secret-internal-value",
        "cache-control": "no-cache",
        "date": "Tue, 08 Oct 2026 00:00:00 GMT",
    })
    assert clean == {
        "content-type": "text/html",
        "location": "/next",
        "cache-control": "no-cache",
        "date": "Tue, 08 Oct 2026 00:00:00 GMT",
    }
