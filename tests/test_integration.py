"""End-to-end integration: device → gateway → mock origin → device.

Everything runs on loopback with local sockets only — offline by construction.
"""
from __future__ import annotations

import time

import pytest

from core import logging as rb_logging
from tests.conftest import (
    CERTS,
    OriginServer,
    capture_gateway_log,
    gateway_config,
    gateway_port,
    parse_http_response,
    raw_request,
    request_bytes,
)


@pytest.fixture(autouse=True)
def _logging_ready():
    rb_logging.setup("debug")


@pytest.fixture()
def origin():
    with OriginServer() as server:
        yield server


@pytest.fixture()
def gateway(running_gateway, origin):
    return running_gateway(gateway_config(origin.port))


# -- happy path -------------------------------------------------------------- #


def test_get_through_path_prefix(gateway, origin):
    port = gateway_port(gateway)
    payload = raw_request(
        port,
        request_bytes("GET", "/http://127.0.0.1:%d/hello" % origin.port, "HTTP/1.0",
                      headers=[("User-Agent", "OldPhone/1.0")]),
    )
    response = parse_http_response(payload)
    assert response["status"] == 200
    body = response["body"].decode("iso-8859-1")
    assert "Hello" in body
    assert "<script" not in body.lower()
    assert "alert" not in body
    assert "onclick" not in body
    assert 'href="#"' in body  # javascript: URL neutralized, link kept
    headers = response["headers"]
    assert headers["content-type"].startswith("text/html")
    assert "charset=iso-8859-1" in headers["content-type"]
    assert headers["connection"] == "close"
    assert int(headers["content-length"]) == len(response["body"])
    assert response["version"] == "HTTP/1.0"


def test_get_through_proxy_style_absolute_form(gateway, origin):
    port = gateway_port(gateway)
    payload = raw_request(
        port,
        request_bytes("GET", "http://127.0.0.1:%d/hello" % origin.port, "HTTP/1.1",
                      headers=[("Host", "127.0.0.1:%d" % origin.port)]),
    )
    response = parse_http_response(payload)
    assert response["status"] == 200
    assert response["version"] == "HTTP/1.1"
    assert b"Hello" in response["body"]


def test_post_body_round_trip(gateway, origin):
    port = gateway_port(gateway)
    payload = raw_request(
        port,
        request_bytes(
            "POST", "/http://127.0.0.1:%d/echo" % origin.port, "HTTP/1.1",
            headers=[("Host", "gw"), ("Content-Type", "text/plain"),
                     ("Content-Length", "9")],
            body=b"ping-pong",
        ),
    )
    response = parse_http_response(payload)
    assert response["status"] == 200
    assert response["body"] == b"echo:ping-pong"
    assert origin.last_headers.get("content-type") == "text/plain"


def test_head_sends_no_body(gateway, origin):
    port = gateway_port(gateway)
    payload = raw_request(
        port, request_bytes("HEAD", "/http://127.0.0.1:%d/hello" % origin.port)
    )
    response = parse_http_response(payload)
    assert response["status"] == 200
    assert response["body"] == b""
    assert int(response["headers"]["content-length"]) > 0


def test_origin_404_passes_through(gateway, origin):
    port = gateway_port(gateway)
    payload = raw_request(
        port, request_bytes("GET", "/http://127.0.0.1:%d/missing" % origin.port)
    )
    assert parse_http_response(payload)["status"] == 404


# -- redirects --------------------------------------------------------------- #


def test_redirect_location_rewritten_and_followable(gateway, origin):
    port = gateway_port(gateway)
    first = parse_http_response(
        raw_request(port, request_bytes("GET", "/http://127.0.0.1:%d/redirect" % origin.port))
    )
    assert first["status"] == 302
    location = first["headers"]["location"]
    assert location == "http://127.0.0.1:%d/http://127.0.0.1:%d/target" % (port, origin.port)

    # The device follows the Location through the gateway (proxy-style).
    second = parse_http_response(raw_request(port, request_bytes("GET", location, "HTTP/1.1",
                                                                 headers=[("Host", "127.0.0.1:%d" % port)])))
    assert second["status"] == 200
    assert second["body"] == b"target-ok"


def test_relative_redirect_resolved(gateway, origin):
    port = gateway_port(gateway)
    first = parse_http_response(
        raw_request(port, request_bytes("GET", "/http://127.0.0.1:%d/relative-redirect" % origin.port))
    )
    assert first["status"] == 302
    assert first["headers"]["location"].endswith("/http://127.0.0.1:%d/target" % origin.port)


# -- policy rejections ------------------------------------------------------- #


def test_unsupported_scheme_rejected(gateway):
    port = gateway_port(gateway)
    payload = raw_request(port, request_bytes("GET", "ftp://example.com/file"))
    response = parse_http_response(payload)
    assert response["status"] == 403
    assert b"PolicyDenied" in response["body"]


def test_scheme_not_in_allowlist_rejected(gateway, origin):
    port = gateway_port(gateway)
    payload = raw_request(
        port, request_bytes("GET", "/https://127.0.0.1:%d/hello" % origin.port)
    )
    assert parse_http_response(payload)["status"] == 403


def test_host_not_allowlisted_rejected_without_dns(gateway):
    port = gateway_port(gateway)
    started = time.monotonic()
    payload = raw_request(port, request_bytes("GET", "/https://not-allowed.test/x"))
    assert parse_http_response(payload)["status"] == 403
    assert time.monotonic() - started < 2.0  # allowlist denies before any DNS


def test_private_range_denied_when_configured(running_gateway, origin):
    config = gateway_config(origin.port, deny_private_ranges=True)
    gw = running_gateway(config)
    payload = raw_request(
        gateway_port(gw),
        request_bytes("GET", "/http://127.0.0.1:%d/hello" % origin.port),
    )
    response = parse_http_response(payload)
    assert response["status"] == 403
    assert b"not public" in response["body"] or b"PolicyDenied" in response["body"]


def test_fetching_the_gateway_itself_rejected(gateway):
    port = gateway_port(gateway)
    payload = raw_request(
        port, request_bytes("GET", "/http://127.0.0.1:%d/hello" % port)
    )
    response = parse_http_response(payload)
    assert response["status"] == 403
    assert b"itself" in response["body"]


def test_gateway_absolute_target_without_prefix_rejected(gateway):
    port = gateway_port(gateway)
    payload = raw_request(
        port, request_bytes("GET", "http://127.0.0.1:%d/nope" % port, "HTTP/1.1",
                            headers=[("Host", "127.0.0.1:%d" % port)])
    )
    assert parse_http_response(payload)["status"] == 400


def test_method_not_allowed(gateway, origin):
    port = gateway_port(gateway)
    payload = raw_request(
        port, request_bytes("DELETE", "/http://127.0.0.1:%d/hello" % origin.port)
    )
    response = parse_http_response(payload)
    assert response["status"] == 405
    assert b"MethodNotAllowed" in response["body"]


# -- limits and malformed input ---------------------------------------------- #


def test_oversized_post_body_rejected(gateway, origin):
    port = gateway_port(gateway)
    body = b"x" * 70000
    payload = raw_request(
        port,
        request_bytes("POST", "/http://127.0.0.1:%d/echo" % origin.port,
                      headers=[("Content-Length", str(len(body)))], body=body),
    )
    assert parse_http_response(payload)["status"] == 413


def test_malformed_request_line_rejected(gateway):
    port = gateway_port(gateway)
    response = parse_http_response(raw_request(port, b"GARBAGE\r\n\r\n"))
    assert response["status"] == 400
    assert b"MalformedRequest" in response["body"]


def test_header_flood_rejected(gateway, origin):
    port = gateway_port(gateway)
    headers = [("X-H%d" % i, "v" * 40) for i in range(100)]
    payload = raw_request(
        port,
        request_bytes("GET", "/http://127.0.0.1:%d/hello" % origin.port,
                      headers=headers),
    )
    assert parse_http_response(payload)["status"] == 431


def test_garbage_bytes_do_not_crash_gateway(gateway, origin):
    port = gateway_port(gateway)
    raw_request(port, b"\x00\x01\x02\x03\r\n\r\n")
    # gateway still serves the next request
    payload = raw_request(
        port, request_bytes("GET", "/http://127.0.0.1:%d/hello" % origin.port)
    )
    assert parse_http_response(payload)["status"] == 200


def test_upstream_timeout_maps_to_504(running_gateway, origin):
    gw = running_gateway(gateway_config(origin.port, upstream_timeout=0.4))
    payload = raw_request(
        gateway_port(gw),
        request_bytes("GET", "/http://127.0.0.1:%d/slow" % origin.port),
        timeout=10.0,
    )
    response = parse_http_response(payload)
    assert response["status"] == 504
    assert b"UpstreamTimeout" in response["body"]


def test_upstream_refused_maps_to_502(running_gateway):
    from tests.conftest import free_port

    dead_port = free_port()
    gw = running_gateway(gateway_config(dead_port))
    payload = raw_request(
        gateway_port(gw),
        request_bytes("GET", "/http://127.0.0.1:%d/x" % dead_port),
    )
    response = parse_http_response(payload)
    assert response["status"] == 502
    assert b"UpstreamFailure" in response["body"]


def test_oversized_origin_response_truncated(running_gateway, origin):
    gw = running_gateway(gateway_config(origin.port, max_response_body=2048))
    payload = raw_request(
        gateway_port(gw),
        request_bytes("GET", "/http://127.0.0.1:%d/big" % origin.port),
    )
    response = parse_http_response(payload)
    assert response["status"] == 200
    assert len(response["body"]) <= 2048
    assert int(response["headers"]["content-length"]) == len(response["body"])


def test_profile_size_limit_enforced_with_marker(running_gateway, origin):
    """Origin body > profile cap (32 KiB) → truncation marker on the device."""
    gw = running_gateway(gateway_config(origin.port, max_response_body=1_048_576))
    payload = raw_request(
        gateway_port(gw),
        request_bytes("GET", "/http://127.0.0.1:%d/big" % origin.port),
    )
    response = parse_http_response(payload)
    assert response["status"] == 200
    assert len(response["body"]) <= 32768
    assert b"[retrobridge: response truncated]" in response["body"]


# -- redaction --------------------------------------------------------------- #


def test_device_authorization_never_reaches_origin_or_logs(gateway, origin):
    with capture_gateway_log() as log_capture:
        port = gateway_port(gateway)
        payload = raw_request(
            port,
            request_bytes(
                "GET", "/http://127.0.0.1:%d/hello" % origin.port,
                headers=[("Authorization", "Bearer device-super-secret-42"),
                         ("Cookie", "sid=device-cookie-secret")],
            ),
        )
    response = parse_http_response(payload)
    assert response["status"] == 200
    sent = origin.last_headers
    assert "authorization" not in sent
    assert "cookie" not in sent
    assert b"device-super-secret-42" not in payload
    assert "device-super-secret-42" not in log_capture.text
    assert "device-cookie-secret" not in log_capture.text
    assert "sid=device-cookie-secret" not in log_capture.text


def test_upstream_set_cookie_never_reaches_device(gateway, origin):
    port = gateway_port(gateway)
    payload = raw_request(
        port, request_bytes("GET", "/http://127.0.0.1:%d/setcookie" % origin.port)
    )
    response = parse_http_response(payload)
    assert response["status"] == 200
    assert b"supersecret123" not in payload
    assert "set-cookie" not in response["headers"]
    assert "x-internal-header" not in response["headers"]


def test_query_strings_and_secrets_not_logged(gateway, origin):
    with capture_gateway_log() as log_capture:
        port = gateway_port(gateway)
        raw_request(
            port,
            request_bytes(
                "GET",
                "/http://127.0.0.1:%d/hello?token=query-secret-value" % origin.port,
            ),
        )
    assert "query-secret-value" not in log_capture.text
    for event in log_capture.events():
        assert "query" not in event


def test_session_lifecycle_logged_with_trace(running_gateway, origin):
    with capture_gateway_log() as log_capture:
        gw = running_gateway(gateway_config(origin.port))
        raw_request(
            gateway_port(gw),
            request_bytes("GET", "/http://127.0.0.1:%d/hello" % origin.port),
        )
    events = log_capture.events()
    startup = [e for e in events if e["event"] == "startup"]
    requests = [e for e in events if e["event"] == "request"]
    assert startup, "startup event missing"
    assert requests, "request event missing"
    event = requests[0]
    assert event["status"] == 200
    assert event["profile"] == "generic-constrained"
    assert len(event["trace"]) == 16
    assert event["method"] == "GET"
    assert event["host"] == "127.0.0.1"


# -- capability selection end to end ----------------------------------------- #


def test_j2me_user_agent_selects_j2me_profile(gateway, origin):
    port = gateway_port(gateway)
    with capture_gateway_log() as log_capture:
        raw_request(
            port,
            request_bytes("GET", "/http://127.0.0.1:%d/hello" % origin.port,
                          headers=[("User-Agent", "Nokia6680/1.0 MIDP/2.0 Profile/MIDP-2.0")]),
        )
    events = [e for e in log_capture.events() if e["event"] == "request"]
    assert events and events[0]["profile"] == "j2me-midp2-generic"


# -- HTTPS upstream (real TLS against local self-signed origin) -------------- #


def test_https_fetch_with_configured_ca(running_gateway, origin):
    https_origin = OriginServer(tls=True)
    with https_origin:
        config = gateway_config(
            https_origin.port,
            allowed_schemes=("https",),
        )
        from dataclasses import replace

        config = replace(config, egress=replace(config.egress, ca_file=str(CERTS / "cert.pem")))
        gw = running_gateway(config)
        payload = raw_request(
            gateway_port(gw),
            request_bytes("GET", "/https://127.0.0.1:%d/hello" % https_origin.port),
        )
    response = parse_http_response(payload)
    assert response["status"] == 200
    assert b"Hello" in response["body"]


def test_https_fetch_without_trust_rejected(running_gateway):
    https_origin = OriginServer(tls=True)
    with https_origin:
        config = gateway_config(https_origin.port, allowed_schemes=("https",))
        gw = running_gateway(config)  # no ca_file: default trust store only
        payload = raw_request(
            gateway_port(gw),
            request_bytes("GET", "/https://127.0.0.1:%d/hello" % https_origin.port),
        )
    response = parse_http_response(payload)
    assert response["status"] == 502  # self-signed cert must NOT be accepted
    assert b"UpstreamFailure" in response["body"]


# -- concurrency smoke ------------------------------------------------------- #


def test_concurrent_sessions_do_not_cross_talk(gateway, origin):
    import threading

    port = gateway_port(gateway)
    results = {}

    def worker(index: int):
        results[index] = raw_request(
            port,
            request_bytes("GET", "/http://127.0.0.1:%d/hello" % origin.port),
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(results) == 8
    for payload in results.values():
        assert parse_http_response(payload)["status"] == 200
