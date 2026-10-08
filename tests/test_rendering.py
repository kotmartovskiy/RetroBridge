"""Device-facing rendering: status lines, header allowlist, error table (§2.4, §7)."""
from __future__ import annotations

import re

import pytest

from core.errors import ERROR_CLASSES, GatewayError, render_error as render_error_body
from tests.conftest import request_bytes


def head_and_body(payload: bytes):
    head, sep, body = payload.partition(b"\r\n\r\n")
    assert sep, "missing header/body separator"
    return head.decode("latin-1"), body


def header_dict(head: str):
    lines = head.split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


def test_status_line_http10(device):
    head, _ = head_and_body(
        device.render_response(200, {"content-type": "text/plain"}, b"hi", version="HTTP/1.0")
    )
    assert head.split("\r\n")[0] == "HTTP/1.0 200 OK"


def test_status_line_http11(device):
    head, _ = head_and_body(
        device.render_response(200, {"content-type": "text/plain"}, b"hi", version="HTTP/1.1")
    )
    assert head.split("\r\n")[0] == "HTTP/1.1 200 OK"


def test_unknown_version_falls_back_to_http10(device):
    head, _ = head_and_body(
        device.render_response(200, {"content-type": "text/plain"}, b"hi", version="HTTP/9.9")
    )
    assert head.startswith("HTTP/1.0 ")


def test_connection_close_always_emitted(device):
    head, _ = head_and_body(device.render_response(200, {}, b"x"))
    assert header_dict(head)["connection"] == "close"


def test_content_length_matches_body(device):
    head, body = head_and_body(
        device.render_response(200, {"content-type": "text/plain"}, b"hello")
    )
    assert header_dict(head)["content-length"] == "5"
    assert body == b"hello"


def test_head_sends_no_body_but_keeps_length(device):
    payload = device.render_response(
        200, {"content-type": "text/plain"}, b"hello", include_body=False
    )
    head, body = head_and_body(payload)
    assert header_dict(head)["content-length"] == "5"
    assert body == b""


def test_204_has_no_body_or_length(device):
    head, body = head_and_body(device.render_response(204, {"content-type": "text/plain"}, b""))
    headers = header_dict(head)
    assert "content-length" not in headers
    assert body == b""


def test_text_content_type_gets_explicit_charset(device):
    head, _ = head_and_body(device.render_response(200, {"content-type": "text/html"}, b"<p>"))
    assert "charset=" in header_dict(head)["content-type"]


def test_binary_content_type_gets_no_charset(device):
    head, _ = head_and_body(
        device.render_response(200, {"content-type": "image/gif"}, b"GIF89a")
    )
    assert "charset" not in header_dict(head)["content-type"]


def test_response_headers_allowlisted(device):
    payload = device.render_response(
        200,
        {
            "content-type": "text/plain",
            "set-cookie": "session=supersecret",
            "server": "Internal/1.0",
            "x-internal": "secret-value",
            "transfer-encoding": "chunked",
        },
        b"data",
    )
    head, body = head_and_body(payload)
    assert "set-cookie" not in head.lower()
    assert "secret" not in head.lower()
    assert "x-internal" not in head.lower()
    assert body == b"data"


def test_header_crlf_injection_neutralized(device):
    head, _ = head_and_body(
        device.render_response(200, {"content-type": "text/plain", "location": "a\r\nX-Evil: 1"},
                               b"x")
    )
    assert "\r\nX-Evil" not in head
    assert header_dict(head)["location"].startswith("a")


def test_error_body_is_tiny_ascii_with_trace(device):
    from core.errors import PolicyDenied

    payload = device.render_failure(PolicyDenied(), "deadbeefcafe0001", version="HTTP/1.0")
    head, body = head_and_body(payload)
    assert head.split("\r\n")[0].startswith("HTTP/1.0 403")
    text = body.decode("ascii")
    assert "PolicyDenied:" in text
    assert "trace: deadbeefcafe0001" in text
    assert len(body) < 200


@pytest.mark.parametrize("code", sorted(ERROR_CLASSES))
def test_error_table_maps_every_class(device, code):
    exc_cls = ERROR_CLASSES[code]
    exc = exc_cls()
    payload = device.render_failure(exc, "0123456789abcdef", version="HTTP/1.1")
    head, body = head_and_body(payload)
    status_line = head.split("\r\n")[0]
    assert status_line.startswith("HTTP/1.1 %d " % exc.status)
    assert code.encode() in body
    assert b"trace: 0123456789abcdef" in body
    # No stack traces, paths or internal hostnames in any device-facing error.
    text = body.decode("latin-1")
    assert "Traceback" not in text
    assert "File \"" not in text
    assert "\\\\" not in text


def test_error_bodies_never_include_detail(device):
    from core.errors import UpstreamFailure

    exc = UpstreamFailure(detail="connect to 10.1.2.3:443 failed")
    _, body = head_and_body(device.render_failure(exc, "trace1234"))
    assert b"10.1.2.3" not in body


def test_rate_limited_carries_retry_after(device):
    from core.errors import RateLimited

    head, _ = head_and_body(device.render_failure(RateLimited(), "trace1234"))
    assert header_dict(head)["retry-after"] == "5"


def test_render_error_body_helper_matches_table():
    from core.errors import MalformedRequest

    body = render_error_body(MalformedRequest(), "abc")
    assert body == b"MalformedRequest: bad request\ntrace: abc\n"


def test_rendered_header_names_are_canonical(device):
    head, _ = head_and_body(
        device.render_response(200, {"content-type": "text/plain", "location": "/x"}, b"")
    )
    names = [line.split(":")[0] for line in head.split("\r\n")[1:] if ":" in line]
    assert "Content-Type" in names
    assert "Location" in names
