"""Bounded WSP connectionless PDU codec.

Phase 2B scope: decode/encode the WSP connectionless envelope needed for a
minimal GET/REPLY bridge. Header-field and WBXML semantics stay at explicit
adapter boundaries; no connection-oriented WTP session state is implemented.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

MAX_PDU_BYTES = 65536
MAX_HEADER_BYTES = 16384
MAX_BODY_BYTES = 49152

PDU_GET = 0x40
PDU_REPLY = 0x44


class WspCodecError(ValueError):
    pass


@dataclass(frozen=True)
class WspPdu:
    transaction_id: int
    pdu_type: int
    headers: bytes
    body: bytes


def _read_uintvar(data: bytes, offset: int) -> Tuple[int, int]:
    value = 0
    for count in range(5):
        if offset >= len(data):
            raise WspCodecError("truncated uintvar")
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset
    raise WspCodecError("uintvar exceeds five octets")


def _write_uintvar(value: int) -> bytes:
    if value < 0 or value > 0x0FFFFFFF:
        raise WspCodecError("uintvar out of range")
    parts = [value & 0x7F]
    value >>= 7
    while value:
        parts.append(value & 0x7F)
        value >>= 7
    out = bytearray()
    for part in reversed(parts[1:]):
        out.append(part | 0x80)
    out.append(parts[0])
    return bytes(out)


def decode_pdu(data: bytes, *, max_pdu_bytes: int = MAX_PDU_BYTES) -> WspPdu:
    if len(data) > max_pdu_bytes:
        raise WspCodecError("PDU exceeds configured limit")
    if len(data) < 3:
        raise WspCodecError("truncated WSP PDU")
    transaction_id = data[0]
    pdu_type = data[1]
    header_len, offset = _read_uintvar(data, 2)
    if header_len > MAX_HEADER_BYTES:
        raise WspCodecError("WSP header block exceeds limit")
    end = offset + header_len
    if end > len(data):
        raise WspCodecError("truncated WSP header block")
    headers = data[offset:end]
    body = data[end:]
    if len(body) > MAX_BODY_BYTES:
        raise WspCodecError("WSP body exceeds limit")
    return WspPdu(transaction_id, pdu_type, headers, body)


def encode_pdu(pdu: WspPdu, *, max_pdu_bytes: int = MAX_PDU_BYTES) -> bytes:
    if not 0 <= pdu.transaction_id <= 255:
        raise WspCodecError("transaction id out of range")
    if not 0 <= pdu.pdu_type <= 255:
        raise WspCodecError("PDU type out of range")
    if len(pdu.headers) > MAX_HEADER_BYTES:
        raise WspCodecError("WSP header block exceeds limit")
    if len(pdu.body) > MAX_BODY_BYTES:
        raise WspCodecError("WSP body exceeds limit")
    encoded = bytes((pdu.transaction_id, pdu.pdu_type))
    encoded += _write_uintvar(len(pdu.headers)) + pdu.headers + pdu.body
    if len(encoded) > max_pdu_bytes:
        raise WspCodecError("encoded PDU exceeds configured limit")
    return encoded


def decode_get(data: bytes) -> WspPdu:
    pdu = decode_pdu(data)
    if pdu.pdu_type != PDU_GET:
        raise WspCodecError("expected WSP GET.req")
    return pdu


def encode_reply(transaction_id: int, headers: bytes, body: bytes) -> bytes:
    return encode_pdu(WspPdu(transaction_id, PDU_REPLY, headers, body))
