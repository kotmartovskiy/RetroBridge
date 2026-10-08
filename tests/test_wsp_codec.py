import pytest

from core.wsp_codec import (
    PDU_GET,
    PDU_REPLY,
    WspCodecError,
    WspPdu,
    decode_get,
    decode_pdu,
    encode_pdu,
    encode_reply,
)


def test_connectionless_get_round_trip():
    raw = encode_pdu(WspPdu(7, PDU_GET, b"\x00\x01", b"/index.wml"))
    pdu = decode_get(raw)
    assert pdu == WspPdu(7, PDU_GET, b"\x00\x01", b"/index.wml")


def test_reply_preserves_transaction_id():
    raw = encode_reply(19, b"\x01", b"ok")
    assert decode_pdu(raw) == WspPdu(19, PDU_REPLY, b"\x01", b"ok")


def test_uintvar_boundary():
    raw = encode_pdu(WspPdu(1, PDU_GET, b"a" * 128, b""))
    assert decode_pdu(raw).headers == b"a" * 128


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x01",
        b"\x01\x40",
        b"\x01\x40\x81",
        b"\x01\x40\x05abc",
    ],
)
def test_malformed_pdus_are_rejected(raw):
    with pytest.raises(WspCodecError):
        decode_pdu(raw)


def test_wrong_pdu_type_rejected_by_get_decoder():
    with pytest.raises(WspCodecError):
        decode_get(encode_reply(1, b"", b""))


def test_oversized_header_rejected():
    with pytest.raises(WspCodecError):
        encode_pdu(WspPdu(1, PDU_GET, b"x" * 16385, b""))


def test_oversized_body_rejected():
    with pytest.raises(WspCodecError):
        encode_pdu(WspPdu(1, PDU_GET, b"", b"x" * 49153))
