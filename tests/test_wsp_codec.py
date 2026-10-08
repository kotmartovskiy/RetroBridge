import pytest

from core.wsp_codec import (
    PDU_GET,
    PDU_REPLY,
    WspCodecError,
    WspGet,
    WspReply,
    decode_get,
    decode_reply,
    encode_get,
    encode_reply,
)


def test_get_round_trip():
    pdu = WspGet(7, b"http://example.test/a", b"\x01\x02")
    assert decode_get(encode_get(pdu)) == pdu


def test_reply_round_trip():
    pdu = WspReply(19, 0x20, b"server\x00", b"body")
    assert decode_reply(encode_reply(pdu)) == pdu


def test_get_uses_uri_length_not_header_length():
    raw = bytes((1, PDU_GET, 3)) + b"/x?" + b"headers"
    assert decode_get(raw) == WspGet(1, b"/x?", b"headers")


def test_reply_header_length_is_uintvar():
    pdu = WspReply(1, 0x20, b"x" * 128, b"")
    assert decode_reply(encode_reply(pdu)) == pdu


@pytest.mark.parametrize("raw", [
    b"", b"\x01", b"\x01\x40", b"\x01\x40\x81", b"\x01\x40\x05abc",
])
def test_malformed_get_is_rejected(raw):
    with pytest.raises(WspCodecError):
        decode_get(raw)


@pytest.mark.parametrize("raw", [
    b"", b"\x01\x04", b"\x01\x04\x20", b"\x01\x04\x20\x81", b"\x01\x04\x20\x05abc",
])
def test_malformed_reply_is_rejected(raw):
    with pytest.raises(WspCodecError):
        decode_reply(raw)


def test_wrong_pdu_type_rejected():
    with pytest.raises(WspCodecError):
        decode_get(encode_reply(WspReply(1, 0x20)))


def test_oversized_uri_rejected():
    with pytest.raises(WspCodecError):
        encode_get(WspGet(1, b"x" * 4097))


def test_oversized_headers_rejected():
    with pytest.raises(WspCodecError):
        encode_reply(WspReply(1, 0x20, b"x" * 16385))


def test_oversized_body_rejected():
    with pytest.raises(WspCodecError):
        encode_reply(WspReply(1, 0x20, b"", b"x" * 49153))
