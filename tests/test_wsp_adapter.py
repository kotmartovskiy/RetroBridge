import pytest

from core.ir import IrResponse
from core.loader import load_device_adapter
from core.profiles import ProfileRegistry
from core.wsp_codec import WspGet, WspReply, decode_reply, encode_get
from tests.conftest import EXAMPLES


@pytest.fixture(scope="module")
def adapter():
    return load_device_adapter("wsp-connectionless")


@pytest.fixture(scope="module")
def profile():
    return ProfileRegistry.load_dir(EXAMPLES / "profiles").get("wap-wml-classic")


def test_get_is_mapped_to_ir_without_transaction_id(adapter, profile):
    raw = encode_get(WspGet(7, b"http://example.test/index.wml"))
    context = adapter.decode_get_to_ir(raw, profile, "trace-1")

    assert context.transaction_id == 7
    assert context.request.method == "GET"
    assert context.request.target.scheme == "http"
    assert context.request.target.host == "example.test"
    assert context.request.target.path == "/index.wml"
    assert context.request.origin.transport == "wsp-connectionless"
    assert context.request.trace.trace_id == "trace-1"


def test_relative_uri_is_rejected(adapter, profile):
    raw = encode_get(WspGet(1, b"/index.wml"))
    with pytest.raises(ValueError, match="absolute URI"):
        adapter.decode_get_to_ir(raw, profile, "trace-2")


def test_ir_response_maps_http_ok_to_wsp_ok(adapter):
    response = IrResponse(
        status=200,
        headers={"content-type": "text/vnd.wap.wml"},
        body=b"<wml/>",
    )
    raw = adapter.encode_ir_response(23, response)
    reply = decode_reply(raw)

    assert isinstance(reply, WspReply)
    assert reply.transaction_id == 23
    assert reply.status == 0x20
    assert reply.headers == b""
    assert reply.body == b"<wml/>"


def test_unmapped_http_status_is_rejected(adapter):
    with pytest.raises(ValueError, match="no fixture-backed WSP mapping"):
        adapter.ir_response_to_reply(1, IrResponse(status=418, headers={}, body=b""))


def test_adapter_contract(adapter):
    assert adapter.id() == "wsp-connectionless/1.0"
    assert adapter.transports() == ("wsp-connectionless",)
