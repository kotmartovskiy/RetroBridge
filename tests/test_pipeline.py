"""Transformation pipeline stage tests (PROTOCOL §5, ARCHITECTURE §7)."""
from __future__ import annotations

import pytest

from core.ir import IrRequest, IrResponse, OriginInfo, Target, Trace
from core.pipeline import (
    KNOWN_STAGES,
    TRUNCATION_MARKER,
    Pipeline,
    decode_upstream_text,
    parse_content_type,
    stage_charset,
    stage_media_type,
    stage_script_strip,
    stage_size,
    TransformState,
)


def make_request(path="/page", scheme="https", host="origin.test", port=443):
    return IrRequest(
        method="GET",
        target=Target(scheme=scheme, host=host, port=port, path=path, query="",
                      raw_path=path, raw_query=""),
        headers={},
        body=b"",
        content_type=None,
        charset=None,
        origin=OriginInfo("http-plain/1.0", path, [], "http-plain"),
        trace=Trace("trace", 1),
    )


def make_response(body=b"", content_type="text/html; charset=utf-8", status=200, **headers):
    merged = {"content-type": content_type}
    merged.update(headers)
    return IrResponse(status=status, headers=merged, body=body)


def state_for(response, profile, **kwargs):
    from core.policy import sanitize_response_headers

    return TransformState(
        profile=profile,
        status=response.status,
        headers=sanitize_response_headers(dict(response.headers)),
        body=bytes(response.body),
        **kwargs,
    )


PLAN = ("media-type", "charset", "script-strip", "size", "location-rewrite")


# -- content-type parsing ---------------------------------------------------- #


def test_parse_content_type():
    assert parse_content_type('text/html; charset="utf-8"') == ("text/html", "utf-8")
    assert parse_content_type("text/html;charset=ISO-8859-1") == ("text/html", "ISO-8859-1")
    assert parse_content_type("") == ("application/octet-stream", None)
    assert parse_content_type("text/plain") == ("text/plain", None)


def test_decode_declared_charset_wins():
    text, charset, notes = decode_upstream_text("café".encode("utf-8"), "utf-8")
    assert text == "café"
    assert charset == "utf-8"
    assert notes == []


def test_decode_without_declaration_assumes_utf8():
    text, charset, notes = decode_upstream_text("hello".encode("utf-8"), None)
    assert (text, charset) == ("hello", "utf-8")
    assert notes == []


def test_decode_invalid_declaration_falls_back():
    text, charset, notes = decode_upstream_text("hello".encode("utf-8"), "bogus-charset")
    assert charset == "utf-8"
    assert any(note.startswith("declared-charset-unsupported") for note in notes)


def test_decode_replaces_bad_bytes():
    text, charset, notes = decode_upstream_text(b"caf\xff!", "utf-8")
    assert "�" in text
    assert any(note.startswith("charset-assumed") or note.startswith("decode-replaced")
               for note in notes)


# -- stage 1: media-type ----------------------------------------------------- #


def test_media_type_markup_passthrough(registry):
    state = state_for(make_response(b"<p>x</p>"), registry.default)
    stage_media_type(state)
    assert state.representation == "text"
    assert state.content_type == "text/html"
    assert state.markup is True
    assert state.text == "<p>x</p>"


def test_media_type_downgrades_unknown_text(registry):
    state = state_for(make_response(b"# hi", content_type="text/markdown"), registry.default)
    stage_media_type(state)
    assert state.content_type == "text/plain"
    assert "downgraded:text/markdown" in state.notes


def test_media_type_placeholder_for_unsupported(registry):
    state = state_for(make_response(b"%PDF-1.4", content_type="application/pdf"),
                      registry.default)
    stage_media_type(state)
    assert state.content_type == "text/plain"
    assert b"unsupported media type: application/pdf" in state.body
    assert "unsupported-media:application/pdf" in state.notes


def test_media_type_binary_for_allowed_image(registry):
    state = state_for(make_response(b"\x89PNG...", content_type="image/png"),
                      registry.default)
    stage_media_type(state)
    assert state.representation == "binary"
    assert state.content_type == "image/png"
    assert state.body == b"\x89PNG..."


def test_media_type_binary_rejected_when_not_allowed(registry):
    state = state_for(make_response(b"....", content_type="image/tiff"),
                      registry.default)
    stage_media_type(state)
    assert state.representation == "text"
    assert "unsupported-media:image/tiff" in state.notes


# -- stage 3: charset -------------------------------------------------------- #


def test_charset_encodes_to_profile_default(registry):
    state = state_for(make_response("héllo ✓".encode("utf-8")), registry.default)
    stage_media_type(state)
    stage_charset(state)
    assert state.headers["content-type"] == "text/html; charset=iso-8859-1"
    assert state.body == "héllo ?".encode("iso-8859-1")  # ✓ replaced, not crashed


def test_charset_preserves_ascii(registry):
    state = state_for(make_response(b"<p>plain</p>"), registry.default)
    stage_media_type(state)
    stage_charset(state)
    assert state.body == b"<p>plain</p>"


def test_charset_stage_ignores_binary(registry):
    state = state_for(make_response(b"\x00\x01", content_type="image/gif"),
                      registry.default)
    stage_media_type(state)
    body_before = state.body
    stage_charset(state)
    assert state.body == body_before
    assert "content-type" not in state.headers or "charset" not in state.headers.get(
        "content-type", "")


# -- stage 5: script stripping ---------------------------------------------- #


SCRIPTED = (
    b"<html><head><title>t</title><script>var x=1;</script></head>"
    b'<body onload="go()" onclick="bad()">Hello '
    b'<a href="javascript:evil()">link</a> <b>bold</b></body></html>'
)


def test_script_strip_removes_script_blocks(registry):
    state = state_for(make_response(SCRIPTED), registry.default)
    stage_media_type(state)
    stage_charset(state)
    stage_script_strip(state)
    body = state.body.decode("iso-8859-1")
    assert "var x=1;" not in body
    assert "<script" not in body.lower()
    assert "scripts-removed" in state.notes


def test_script_strip_removes_event_handlers(registry):
    state = state_for(make_response(SCRIPTED), registry.default)
    stage_media_type(state)
    stage_charset(state)
    stage_script_strip(state)
    body = state.body.decode("iso-8859-1")
    assert "onload" not in body
    assert "onclick" not in body


def test_script_strip_neutralizes_javascript_urls(registry):
    state = state_for(make_response(SCRIPTED), registry.default)
    stage_media_type(state)
    stage_charset(state)
    stage_script_strip(state)
    body = state.body.decode("iso-8859-1")
    assert "javascript:evil" not in body
    assert 'href="#"' in body
    assert "link" in body  # page stays navigable


def test_script_strip_keeps_plain_text(registry):
    state = state_for(make_response(b"no markup here at all"),
                      registry.default)
    stage_media_type(state)
    stage_charset(state)
    before = state.body
    stage_script_strip(state)
    assert state.body == before
    assert "scripts-removed" not in state.notes


def test_script_strip_skips_non_markup_text(registry):
    state = state_for(make_response(b"var a = '<script>x</script>';",
                                    content_type="text/plain"), registry.default)
    stage_media_type(state)
    stage_charset(state)
    stage_script_strip(state)
    assert state.body == b"var a = '<script>x</script>';"
    assert "scripts-removed" not in state.notes


# -- stage 7: size enforcement ---------------------------------------------- #


def test_size_under_limit_untouched(registry):
    state = state_for(make_response(b"<p>small</p>"), registry.default)
    stage_media_type(state)
    stage_charset(state)
    stage_size(state)
    assert state.body == b"<p>small</p>"
    assert state.truncated is False


def test_size_truncates_text_with_marker(registry):
    payload = b"<html><body>" + b"a" * 40000 + b"</body></html>"
    state = state_for(make_response(payload), registry.default)
    stage_media_type(state)
    stage_charset(state)
    stage_size(state)
    cap = registry.default.content.max_body_bytes
    assert len(state.body) <= cap
    assert state.body.endswith(TRUNCATION_MARKER.encode("iso-8859-1"))
    assert "truncated" in state.notes
    assert state.truncated is True


def test_size_truncation_does_not_end_mid_tag(registry):
    payload = b"<html>" + b"<p>words here</p>" * 5000 + b"</html>"
    state = state_for(make_response(payload), registry.default)
    stage_media_type(state)
    stage_charset(state)
    stage_size(state)
    cut = state.body[: -len(TRUNCATION_MARKER.encode("iso-8859-1"))]
    # No dangling '<' after the final '>' — truncation never splits a tag open.
    assert cut.rfind(b"<") < cut.rfind(b">")


def test_size_replaces_oversize_binary(registry):
    state = state_for(make_response(b"B" * (registry.default.content.max_body_bytes + 10),
                                    content_type="image/gif"), registry.default)
    stage_media_type(state)
    stage_size(state)
    assert state.representation == "text" or b"exceeds device limit" in state.body
    assert len(state.body) <= registry.default.content.max_body_bytes
    assert "oversize-binary" in state.notes


# -- location rewriting ------------------------------------------------------ #


def run_location(response, gateway_base="http://127.0.0.1:8080", request=None):
    pipeline = Pipeline.from_plan(("media-type", "charset", "location-rewrite"))
    return pipeline.run(response, _profile_for(response), request=request or make_request(),
                        gateway_base=gateway_base)


def _profile_for(response):
    from core.profiles import CapabilityProfile

    return CapabilityProfile.from_dict({
        "schema": "retrobridge/capability-profile@1",
        "id": "unit",
        "content": {"charsets": ["utf-8"], "default_charset": "utf-8"},
        "version": "0.1.0",
    })


def test_location_absolute_rewritten():
    response = make_response(b"", content_type="text/html", status=302,
                             location="https://other.test/x?y=1")
    result = run_location(response)
    assert result.headers["location"] == "http://127.0.0.1:8080/https://other.test/x?y=1"


def test_location_relative_resolved_against_request():
    response = make_response(b"", content_type="text/html", status=302,
                             location="../next/page")
    result = run_location(response, request=make_request(path="/a/b/c"))
    assert result.headers["location"] == "http://127.0.0.1:8080/https://origin.test/a/next/page"


def test_location_root_relative_resolved_against_host():
    response = make_response(b"", content_type="text/html", status=302,
                             location="/somewhere")
    result = run_location(response, request=make_request(path="/a/b"))
    assert result.headers["location"] == "http://127.0.0.1:8080/https://origin.test/somewhere"


def test_location_not_rewritten_on_200():
    response = make_response(b"ok", content_type="text/plain", location="/x")
    result = run_location(response)
    assert result.headers.get("location") == "/x"
    assert "location-rewritten" not in result.meta.get("transform_notes", [])


def test_location_not_rewritten_without_gateway_base():
    response = make_response(b"", content_type="text/html", status=302,
                             location="https://other.test/x")
    result = run_location(response, gateway_base="")
    assert result.headers["location"] == "https://other.test/x"


def test_location_unsupported_scheme_left_alone():
    response = make_response(b"", content_type="text/html", status=302,
                             location="ftp://other.test/x")
    result = run_location(response)
    assert result.headers["location"] == "ftp://other.test/x"


# -- pipeline plumbing ------------------------------------------------------- #


def test_unknown_stage_rejected():
    with pytest.raises(ValueError):
        Pipeline.from_plan(("media-type", "no-such-stage"))


def test_empty_plan_rejected():
    with pytest.raises(ValueError):
        Pipeline(())


def test_all_known_stages_registered():
    from core.pipeline import STAGE_REGISTRY

    for name in KNOWN_STAGES:
        if name == "location-rewrite":
            continue
        assert name in STAGE_REGISTRY


def test_pipeline_strips_upstream_set_cookie(registry):
    response = make_response(b"body", content_type="text/plain",
                             **{"set-cookie": "sid=secret"})
    pipeline = Pipeline.from_plan(PLAN)
    result = pipeline.run(response, registry.default, request=make_request())
    assert "set-cookie" not in result.headers
    assert b"secret" not in result.body


def test_pipeline_reports_transform_meta(registry):
    response = make_response(SCRIPTED)
    pipeline = Pipeline.from_plan(PLAN)
    result = pipeline.run(response, registry.default, request=make_request())
    assert result.meta["representation"] == "text"
    assert result.meta["transformed"] == list(PLAN)
    assert "scripts-removed" in result.meta["transform_notes"]


def test_pipeline_drops_upstream_content_length(registry):
    response = make_response(b"12345", content_type="text/plain",
                             **{"content-length": "999999"})
    pipeline = Pipeline.from_plan(PLAN)
    result = pipeline.run(response, registry.default, request=make_request())
    assert "content-length" not in result.headers


def test_pipeline_status_preserved(registry):
    response = make_response(b"", content_type="text/html", status=404)
    pipeline = Pipeline.from_plan(PLAN)
    result = pipeline.run(response, registry.default, request=make_request())
    assert result.status == 404
