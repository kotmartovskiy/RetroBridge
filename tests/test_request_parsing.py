"""Parsing tests: request line, targets, headers, bodies, limits, malformations."""
from __future__ import annotations

import pytest

from core.errors import (
    HeaderTooLarge,
    MalformedRequest,
    MethodNotAllowed,
    PolicyDenied,
    RequestTooLarge,
)
from tests.conftest import GATEWAY_AUTHORITY, parse, request_bytes


def test_valid_absolute_get(device):
    parsed = parse(request_bytes("GET", "https://example.com/a/b?x=1", "HTTP/1.0",
                                 headers=[("User-Agent", "Test/1.0")]), device)
    assert parsed.method == "GET"
    assert parsed.target == b"https://example.com/a/b?x=1"
    assert parsed.version == "HTTP/1.0"
    assert parsed.headers == [("user-agent", "Test/1.0")]


def test_http11_accepted(device):
    parsed = parse(request_bytes("GET", "https://example.com/", "HTTP/1.1",
                                 headers=[("Host", "example.com")]), device)
    assert parsed.version == "HTTP/1.1"


def test_lowercase_method_normalized(device):
    parsed = parse(request_bytes("get", "https://example.com/"), device)
    assert parsed.method == "GET"


def test_head_and_post_methods_allowed(device):
    for method in ("HEAD", "POST"):
        parsed = parse(request_bytes(method, "https://example.com/"), device)
        assert parsed.method == method


@pytest.mark.parametrize("method", ["DELETE", "PUT", "OPTIONS", "TRACE", "PATCH"])
def test_unsupported_method_raises_405(device, method):
    with pytest.raises(MethodNotAllowed) as excinfo:
        parse(request_bytes(method, "https://example.com/"), device)
    assert excinfo.value.status == 405


def test_bad_method_token(device):
    with pytest.raises(MalformedRequest):
        parse(b"G1T https://example.com/ HTTP/1.0\r\n\r\n", device)


def test_unsupported_version(device):
    with pytest.raises(MalformedRequest):
        parse(request_bytes("GET", "https://example.com/", "HTTP/2.0"), device)


def test_http09_version_rejected(device):
    with pytest.raises(MalformedRequest):
        parse(request_bytes("GET", "https://example.com/", "HTTP/0.9"), device)


def test_request_line_wrong_shape(device):
    with pytest.raises(MalformedRequest):
        parse(b"GET /only-two-parts\r\n\r\n", device)


def test_empty_stream(device):
    with pytest.raises(MalformedRequest):
        parse(b"", device)


def test_extra_spaces_in_request_line_tolerated(device):
    parsed = parse(b"GET   https://example.com/   HTTP/1.0\r\n\r\n", device)
    assert parsed.method == "GET"


def test_extra_headers_parsed_and_lowercased(device):
    parsed = parse(
        request_bytes(
            "GET", "https://example.com/", "HTTP/1.1",
            headers=[("Host", "example.com"), ("X-Custom", "  spaced  "), ("Accept", "*/*")],
        ),
        device,
    )
    assert parsed.header_map["host"] == "example.com"
    assert parsed.header_map["x-custom"] == "spaced"
    assert parsed.header_map["accept"] == "*/*"


def test_duplicate_headers_joined(device):
    parsed = parse(
        request_bytes("GET", "https://example.com/", headers=[("Accept", "text/html"),
                                                               ("Accept", "*/*")]),
        device,
    )
    assert parsed.header_map["accept"] == "text/html, */*"


def test_obs_fold_continuation_tolerated(device):
    raw = (
        b"GET https://example.com/ HTTP/1.0\r\n"
        b"X-Long: first\r\n second\r\n"
        b"\r\n"
    )
    parsed = parse(raw, device)
    assert parsed.header_map["x-long"] == "first second"


def test_orphaned_continuation_rejected(device):
    raw = b"GET https://example.com/ HTTP/1.0\r\n not-a-continuation\r\n\r\n"
    with pytest.raises(MalformedRequest):
        parse(raw, device)


def test_header_without_colon_rejected(device):
    raw = b"GET https://example.com/ HTTP/1.0\r\nBadHeaderNoColon\r\n\r\n"
    with pytest.raises(MalformedRequest):
        parse(raw, device)


def test_header_name_with_space_rejected(device):
    raw = b"GET https://example.com/ HTTP/1.0\r\nBad Name: value\r\n\r\n"
    with pytest.raises(MalformedRequest):
        parse(raw, device)


def test_crlf_injection_in_header_value_rejected(device):
    raw = b"GET https://example.com/ HTTP/1.0\r\nX-Evil: ok\x01bad\r\n\r\n"
    with pytest.raises(MalformedRequest):
        parse(raw, device)


def test_header_count_limit(device):
    headers = [("X-H%d" % index, "v") for index in range(10)]
    limits = device.RequestLimits(max_header_count=5)
    with pytest.raises(HeaderTooLarge) as excinfo:
        parse(request_bytes("GET", "https://example.com/", headers=headers), device, limits)
    assert excinfo.value.status == 431


def test_total_header_bytes_limit(device):
    headers = [("X-H%d" % index, "y" * 50) for index in range(10)]
    limits = device.RequestLimits(max_request_header_bytes=200)
    with pytest.raises(HeaderTooLarge):
        parse(request_bytes("GET", "https://example.com/", headers=headers), device, limits)


def test_request_line_too_long(device):
    limits = device.RequestLimits(max_request_line_bytes=32)
    with pytest.raises(HeaderTooLarge):
        parse(request_bytes("GET", "https://example.com/" + "a" * 100), device, limits)


def test_single_header_line_too_long(device):
    limits = device.RequestLimits(max_header_line_bytes=16, max_request_header_bytes=8192)
    with pytest.raises(HeaderTooLarge):
        parse(
            request_bytes("GET", "https://example.com/",
                          headers=[("X-Long", "z" * 100)]),
            device, limits,
        )


def test_body_read_with_content_length(device):
    parsed = parse(
        request_bytes("POST", "https://example.com/", headers=[("Content-Length", "5")],
                      body=b"hello"),
        device,
    )
    assert parsed.body == b"hello"


def test_missing_content_length_means_empty_body(device):
    parsed = parse(request_bytes("POST", "https://example.com/", body=b"ignored"), device)
    assert parsed.body == b""


def test_invalid_content_length(device):
    with pytest.raises(MalformedRequest):
        parse(
            request_bytes("POST", "https://example.com/", headers=[("Content-Length", "abc")],
                          body=b"x"),
            device,
        )


def test_conflicting_content_length(device):
    raw = (
        b"POST https://example.com/ HTTP/1.0\r\n"
        b"Content-Length: 5\r\nContent-Length: 6\r\n\r\nhello"
    )
    with pytest.raises(MalformedRequest):
        parse(raw, device)


def test_body_over_limit(device):
    limits = device.RequestLimits(max_body_bytes=4)
    with pytest.raises(RequestTooLarge) as excinfo:
        parse(
            request_bytes("POST", "https://example.com/", headers=[("Content-Length", "10")],
                          body=b"0123456789"),
            device, limits,
        )
    assert excinfo.value.status == 413


def test_truncated_body_rejected(device):
    raw = (
        b"POST https://example.com/ HTTP/1.0\r\n"
        b"Content-Length: 100\r\n\r\nshort"
    )
    with pytest.raises(MalformedRequest):
        parse(raw, device)


def test_chunked_transfer_encoding_rejected(device):
    raw = (
        b"POST https://example.com/ HTTP/1.0\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
    )
    with pytest.raises(MalformedRequest):
        parse(raw, device)


# -- target normalization ---------------------------------------------------- #


def test_percent_decoding_strict_ok(device):
    from core.loader import load_device_adapter  # noqa: F401 (import path check)

    target = device.parse_request_target(b"https://ex.test/a%20b?q=1%2B2", "utf-8")
    assert target.path == "/a b"
    assert target.query == "q=1+2"
    assert target.raw_path == "/a%20b"
    assert target.raw_query == "q=1%2B2"


@pytest.mark.parametrize("raw", [b"https://ex.test/%zz", b"https://ex.test/%A", b"https://ex.test/x%"])
def test_malformed_percent_encoding_rejected(device, raw):
    with pytest.raises(MalformedRequest):
        device.parse_request_target(raw, "utf-8")


def test_raw_non_ascii_bytes_reencoded(device):
    target = device.parse_request_target(b"https://ex.test/caf\xe9", "utf-8")
    assert target.raw_path == "/caf%C3%A9" or target.raw_path == "/caf%E9"
    assert target.path.startswith("/caf")


def test_path_prefix_form(device):
    target = device.parse_request_target(b"/https://ex.test/path?q=1", "utf-8",
                                         gateway_authority=GATEWAY_AUTHORITY)
    assert (target.scheme, target.host, target.port, target.path) == (
        "https", "ex.test", 443, "/path")


def test_scheme_relative_prefix_form(device):
    target = device.parse_request_target(b"//ex.test/path", "utf-8")
    assert target.scheme == "http"
    assert target.host == "ex.test"
    assert target.path == "/path"


def test_origin_form_without_prefix_rejected(device):
    with pytest.raises(MalformedRequest):
        device.parse_request_target(b"/plain/path", "utf-8",
                                    gateway_authority=GATEWAY_AUTHORITY)


def test_absolute_target_aimed_at_gateway_unwraps(device):
    target = device.parse_request_target(
        b"http://127.0.0.1:8080/https://ex.test/deep", "utf-8",
        gateway_authority=GATEWAY_AUTHORITY,
    )
    assert target.host == "ex.test"
    assert target.path == "/deep"


def test_absolute_target_aimed_at_gateway_without_prefix_rejected(device):
    with pytest.raises(MalformedRequest):
        device.parse_request_target(b"http://127.0.0.1:8080/nope", "utf-8",
                                    gateway_authority=GATEWAY_AUTHORITY)


def test_userinfo_in_target_rejected(device):
    with pytest.raises(PolicyDenied):
        device.parse_request_target(b"https://user:pass@ex.test/", "utf-8")


def test_explicit_port_parsed(device):
    target = device.parse_request_target(b"https://ex.test:8443/x", "utf-8")
    assert target.port == 8443


def test_bad_port_rejected(device):
    with pytest.raises(MalformedRequest):
        device.parse_request_target(b"https://ex.test:notaport/x", "utf-8")
    with pytest.raises(MalformedRequest):
        device.parse_request_target(b"https://ex.test:99999/x", "utf-8")


def test_fragment_stripped(device):
    target = device.parse_request_target(b"https://ex.test/x#frag", "utf-8")
    assert target.path == "/x"
    assert "#" not in target.raw_path


def test_empty_path_becomes_root(device):
    target = device.parse_request_target(b"https://ex.test", "utf-8")
    assert target.path == "/"
    assert target.raw_origin_form == "/"


def test_to_ir_strips_sensitive_headers_and_redacts_origin(device, registry):
    parsed = parse(
        request_bytes(
            "GET", "https://example.com/", headers=[
                ("Authorization", "Bearer topsecret"),
                ("Cookie", "sid=12345"),
                ("Content-Type", "text/plain"),
                ("X-Forwarded-For", "10.0.0.1"),
            ],
        ),
        device,
    )
    profile = registry.default
    ir = device.to_ir(parsed, profile, "trace1", gateway_authority=(GATEWAY_AUTHORITY,))
    assert "authorization" not in ir.headers
    assert "cookie" not in ir.headers
    assert "x-forwarded-for" not in ir.headers
    assert ir.headers.get("content-type") == "text/plain"
    values = dict(ir.origin.raw_headers)
    assert values["authorization"] == "<redacted>"
    assert values["cookie"] == "<redacted>"


def test_to_ir_enforces_profile_header_limit(device, registry):
    from core.profiles import CapabilityProfile

    tiny = CapabilityProfile.from_dict({
        "schema": "retrobridge/capability-profile@1",
        "id": "tiny",
        "description": "tiny limits",
        "transport": {"max_request_header_bytes": 64},
        "content": {"charsets": ["us-ascii"], "default_charset": "us-ascii",
                    "max_body_bytes": 100},
        "version": "0.1.0",
    })
    parsed = parse(
        request_bytes("GET", "https://example.com/",
                      headers=[("X-Big", "v" * 200)]),
        device,
    )
    from core.errors import HeaderTooLarge

    with pytest.raises(HeaderTooLarge):
        device.to_ir(parsed, tiny, "t")


def test_to_ir_enforces_profile_body_limit(device, registry):
    from core.errors import RequestTooLarge
    from core.profiles import CapabilityProfile

    tiny = CapabilityProfile.from_dict({
        "schema": "retrobridge/capability-profile@1",
        "id": "tiny2",
        "description": "tiny body",
        "content": {"charsets": ["us-ascii"], "default_charset": "us-ascii",
                    "max_body_bytes": 4},
        "version": "0.1.0",
    })
    parsed = parse(
        request_bytes("POST", "https://example.com/",
                      headers=[("Content-Length", "10")], body=b"0123456789"),
        device,
    )
    with pytest.raises(RequestTooLarge):
        device.to_ir(parsed, tiny, "t")
