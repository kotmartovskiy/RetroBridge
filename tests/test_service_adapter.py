"""Service adapter contract: pure prepare() and bounded execute() against a
local mock origin (offline; no live Internet)."""
from __future__ import annotations

import gzip
import socket

import pytest

from core.errors import PolicyDenied, UpstreamFailure, UpstreamTimeout
from core.ir import IrRequest, OriginInfo, Target, Trace
from core.policy import EgressPolicy
from tests.conftest import OriginServer


def make_request(method="GET", scheme="https", host="origin.test", port=443,
                 path="/x", query="", body=b"", headers=None):
    return IrRequest(
        method=method,
        target=Target(scheme=scheme, host=host, port=port, path=path, query=query,
                      raw_path=path, raw_query=query),
        headers=headers or {},
        body=body,
        content_type=None,
        charset=None,
        origin=OriginInfo("http-plain/1.0", path, [], "http-plain"),
        trace=Trace("trace", 1),
    )


def make_decision(scheme="https", host="origin.test", port=443, ip="93.184.216.34"):
    from core.policy import EgressDecision

    return EgressDecision(scheme=scheme, host=host, port=port, pinned_ip=ip)


def ctx(**overrides):
    defaults = dict(trace_id="trace", timeout_s=3.0, max_response_bytes=1_000_000,
                    max_response_header_bytes=16_384)
    defaults.update(overrides)
    return SERVICE_ADAPTER.AdapterContext(**defaults)


# Shared module under test (loaded once via the adapter loader).
from core.loader import load_service_adapter  # noqa: E402

SERVICE_ADAPTER = load_service_adapter("origin-http")


# -- matches ----------------------------------------------------------------- #


def test_matches_allowlisted_https(service):
    policy = EgressPolicy(allowed_schemes=("https",), allowed_hosts=("origin.test",))
    assert service.matches(make_request(scheme="https"), policy) is True


def test_matches_rejects_unlisted_host(service):
    policy = EgressPolicy(allowed_schemes=("https",), allowed_hosts=("origin.test",))
    assert service.matches(make_request(host="evil.test"), policy) is False


def test_matches_rejects_unlisted_scheme(service):
    policy = EgressPolicy(allowed_schemes=("https",), allowed_hosts=("origin.test",))
    assert service.matches(make_request(scheme="http", port=80), policy) is False


def test_matches_rejects_unlisted_port(service):
    policy = EgressPolicy(allowed_schemes=("https",), allowed_hosts=("origin.test",),
                          allowed_ports=(443,))
    assert service.matches(make_request(port=8443), policy) is False


# -- prepare ----------------------------------------------------------------- #


def test_prepare_shape(service):
    outbound = service.prepare(
        make_request(headers={"user-agent": "OldPhone/1.0", "accept": "text/html"}),
        make_decision(),
        service.AdapterContext(trace_id="t"),
    )
    assert outbound.method == "GET"
    assert outbound.scheme == "https"
    assert outbound.host == "origin.test"
    assert outbound.port == 443
    assert outbound.target == "/x"
    assert ("Host", "origin.test") in outbound.headers
    assert ("User-Agent", "OldPhone/1.0") in outbound.headers
    assert ("Accept", "text/html") in outbound.headers
    assert ("Accept-Encoding", "identity") in outbound.headers
    assert ("Connection", "close") in outbound.headers
    assert outbound.pinned_ip == "93.184.216.34"
    assert outbound.body == b""


def test_prepare_host_header_includes_non_default_port(service):
    outbound = service.prepare(
        make_request(scheme="http", port=8080),
        make_decision(scheme="http", port=8080),
        service.AdapterContext(trace_id="t"),
    )
    assert ("Host", "origin.test:8080") in outbound.headers


def test_prepare_uses_gateway_user_agent_when_device_silent(service):
    outbound = service.prepare(
        make_request(),
        make_decision(),
        service.AdapterContext(trace_id="t"),
    )
    assert ("User-Agent", service.GATEWAY_USER_AGENT) in outbound.headers


def test_prepare_sends_body_only_for_post(service):
    post = service.prepare(
        make_request(method="POST", body=b"payload"),
        make_decision(),
        service.AdapterContext(trace_id="t"),
    )
    assert post.body == b"payload"
    assert ("Content-Length", "7") in post.headers

    get = service.prepare(
        make_request(method="GET", body=b"payload"),
        make_decision(),
        service.AdapterContext(trace_id="t"),
    )
    assert get.body == b""
    assert ("Content-Length", "7") not in get.headers


def test_prepare_rejects_unsupported_scheme(service):
    with pytest.raises(PolicyDenied):
        service.prepare(
            make_request(scheme="ftp", port=21),
            make_decision(scheme="ftp", port=21),
            service.AdapterContext(trace_id="t"),
        )


def test_prepare_carries_limits(service):
    ctx = service.AdapterContext(trace_id="t", timeout_s=2.5,
                                 max_response_bytes=1234,
                                 max_response_header_bytes=4321,
                                 ca_file="/tmp/ca.pem")
    outbound = service.prepare(make_request(), make_decision(), ctx)
    assert outbound.timeout_s == 2.5
    assert outbound.max_response_bytes == 1234
    assert outbound.max_response_header_bytes == 4321
    assert outbound.ca_file == "/tmp/ca.pem"


# -- execute against a local mock origin ------------------------------------- #


def http_decision(origin):
    from core.policy import EgressDecision

    return EgressDecision(scheme="http", host=origin.host, port=origin.port,
                          pinned_ip="127.0.0.1")


def test_execute_fetches_from_local_origin(service):
    with OriginServer() as origin:
        response = service.fetch(
            make_request(scheme="http", host=origin.host, port=origin.port, path="/hello"),
            http_decision(origin),
            ctx(),
        )
    assert response.status == 200
    assert b"Hello" in response.body
    assert response.meta["upstream_host"] == origin.host
    assert response.meta["bytes_in"] == len(response.body)
    assert response.meta["cache"] == "bypass"
    assert "set-cookie" not in response.headers


def test_execute_post_echoes_body(service):
    with OriginServer() as origin:
        response = service.fetch(
            make_request(method="POST", scheme="http", host=origin.host,
                         port=origin.port, path="/echo", body=b"ping-pong",
                         headers={"content-type": "text/plain"}),
            http_decision(origin),
            ctx(),
        )
    assert response.status == 200
    assert response.body == b"echo:ping-pong"


def test_execute_truncates_oversize_body(service):
    with OriginServer() as origin:
        response = service.fetch(
            make_request(scheme="http", host=origin.host, port=origin.port, path="/big"),
            http_decision(origin),
            ctx(max_response_bytes=1000),
        )
    assert response.status == 200
    assert len(response.body) == 1000
    assert response.meta["upstream_truncated"] is True


def test_execute_decodes_gzip_response(service):
    with OriginServer() as origin:
        response = service.fetch(
            make_request(scheme="http", host=origin.host, port=origin.port, path="/gzip"),
            http_decision(origin),
            ctx(),
        )
    assert response.body == b"gzip-ok"
    assert "content-encoding" not in response.headers


def test_execute_rejects_unknown_content_encoding(service):
    with OriginServer() as origin:
        with pytest.raises(UpstreamFailure):
            service.fetch(
                make_request(scheme="http", host=origin.host, port=origin.port, path="/br"),
                http_decision(origin),
                ctx(),
            )


def test_execute_rejects_oversize_response_headers(service):
    with OriginServer() as origin:
        with pytest.raises(UpstreamFailure):
            service.fetch(
                make_request(scheme="http", host=origin.host, port=origin.port,
                             path="/hugeheaders"),
                http_decision(origin),
                ctx(max_response_header_bytes=2048),
            )


def test_execute_connection_refused_maps_to_upstream_failure(service):
    from core.policy import EgressDecision
    from tests.conftest import free_port

    port = free_port()  # nothing is listening here
    with pytest.raises(UpstreamFailure):
        service.fetch(
            make_request(scheme="http", host="127.0.0.1", port=port, path="/"),
            EgressDecision(scheme="http", host="127.0.0.1", port=port,
                           pinned_ip="127.0.0.1"),
            ctx(),
        )


def test_execute_timeout_maps_to_upstream_timeout(service):
    with OriginServer() as origin:
        with pytest.raises(UpstreamTimeout):
            service.fetch(
                make_request(scheme="http", host=origin.host, port=origin.port, path="/slow"),
                http_decision(origin),
                ctx(timeout_s=0.4),
            )


def test_execute_refuses_unsupported_scheme(service, monkeypatch):
    with pytest.raises(PolicyDenied):
        service.execute(
            type("Out", (), {
                "scheme": "ftp", "host": "origin.test", "port": 21, "target": "/",
                "method": "GET", "headers": (), "body": b"", "timeout_s": 1.0,
                "max_response_bytes": 100, "max_response_header_bytes": 100,
                "pinned_ip": "127.0.0.1", "ca_file": None,
            })(),
            ctx(),
        )


def test_execute_default_tls_verifies_certificates(service):
    """Upstream TLS uses a default-strength context (no weakening for devices)."""
    import ssl

    context = service._tls_context(None)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_policy_json_mirrors_adapter_defaults(service):
    """policy.json stays in sync with DEFAULT_EGRESS (documented contract)."""
    import json
    from pathlib import Path

    document = json.loads(
        (Path(__file__).resolve().parent.parent / "service-adapters" / "origin-http"
         / "policy.json").read_text(encoding="utf-8")
    )
    assert document["egress"] == service.DEFAULT_EGRESS
    assert document["adapter"] == service.ADAPTER_ID
    assert document["methods"] == ["GET", "HEAD", "POST"]
