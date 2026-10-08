"""Pure routing decision (ARCHITECTURE §3.2 / §4 step 5)."""
from __future__ import annotations

import pytest

from core.errors import PolicyDenied
from core.ir import IrRequest, OriginInfo, Target, Trace
from core.router import DEFAULT_PLAN, SCHEME_ROUTES, route


def make_request(scheme, host="origin.test", port=443):
    return IrRequest(
        method="GET",
        target=Target(scheme=scheme, host=host, port=port, path="/", query="",
                      raw_path="/", raw_query=""),
        headers={},
        body=b"",
        content_type=None,
        charset=None,
        origin=OriginInfo("http-plain/1.0", "/", [], "http-plain"),
        trace=Trace("t", 1),
    )


def test_https_routes_to_origin_http():
    result = route(make_request("https"))
    assert result.service_adapter_id == "origin-http"
    assert result.plan == DEFAULT_PLAN


def test_http_routes_to_origin_http():
    assert route(make_request("http")).service_adapter_id == "origin-http"


def test_scheme_is_case_insensitive():
    assert route(make_request("HTTPS")).service_adapter_id == "origin-http"


def test_unsupported_scheme_rejected():
    with pytest.raises(PolicyDenied):
        route(make_request("ftp"))
    with pytest.raises(PolicyDenied):
        route(make_request("gopher"))


def test_default_plan_is_phase1_stages():
    assert DEFAULT_PLAN == (
        "media-type",
        "charset",
        "script-strip",
        "size",
        "location-rewrite",
    )


def test_custom_plan_is_honored():
    result = route(make_request("https"), plan=("media-type",))
    assert result.plan == ("media-type",)


def test_scheme_routes_table_is_small_and_explicit():
    assert set(SCHEME_ROUTES) == {"http", "https"}
