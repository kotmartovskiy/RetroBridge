"""Bounded connectionless WSP request/reply codec.

Only the WSP PDU fields needed for a minimal GET/REPLY bridge are implemented.
Connection-oriented WTP/WSP session state is deliberately outside this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

MAX_PDU_BYTES = 65536
MAX_URI_BYTES = 4096
MAX_HEADER_BYTES = 16384
MAX_BODY_BYTES = 49152

PDU_GET = 0x40
PDU_REPLY = 0x04


class WspCodecError(ValueError):
    pass


@dataclass(frozen=True)
class WspGet:
    transaction_id: int
    uri: bytes
    headers: bytes = b""


@dataclass(frozen=True)
class WspReply:
    transaction_id: int
    status: int
    headers: bytes = b""
    body: bytes = b""


def _read_uintvar(data: bytes, offset: int) -> Tuple[int, int]:
    value = 0
    for _ in range(5):
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


def _check_pdu_size(data: bytes, limit: int) -> None:
    if len(data) > limit:
        raise WspCodecError("PDU exceeds configured limit")


def decode_get(data: bytes, *, max_pdu_bytes: int = MAX_PDU_BYTES) -> WspGet:
    _check_pdu_size(data, max_pdu_bytes)
    if len(data) < 3 or data[1] != PDU_GET:
        raise WspCodecError("expected WSP GET.req")
    uri_len, offset = _read_uintvar(data, 2)
    if uri_len > MAX_URI_BYTES:
        raise WspCodecError("URI exceeds configured limit")
    end_uri = offset + uri_len
    if end_uri > len(data):
        raise WspCodecError("truncated WSP GET URI")
    uri = data[offset:end_uri]
    headers = data[end_uri:]
    if len(headers) > MAX_HEADER_BYTES:
        raise WspCodecError("WSP header block exceeds limit")
    return WspGet(data[0], uri, headers)


def encode_get(pdu: WspGet, *, max_pdu_bytes: int = MAX_PDU_BYTES) -> bytes:
    _validate_tid(pdu.transaction_id)
    if len(pdu.uri) > MAX_URI_BYTES:
        raise WspCodecError("URI exceeds configured limit")
    if len(pdu.headers) > MAX_HEADER_BYTES:
        raise WspCodecError("WSP header block exceeds limit")
    data = bytes((pdu.transaction_id, PDU_GET))
    data += _write_uintvar(len(pdu.uri)) + pdu.uri + pdu.headers
    _check_pdu_size(data, max_pdu_bytes)
    return data


def decode_reply(data: bytes, *, max_pdu_bytes: int = MAX_PDU_BYTES) -> WspReply:
    _check_pdu_size(data, max_pdu_bytes)
    if len(data) < 4 or data[1] != PDU_REPLY:
        raise WspCodecError("expected WSP Reply")
    status = data[2]
    header_len, offset = _read_uintvar(data, 3)
    if header_len > MAX_HEADER_BYTES:
        raise WspCodecError("WSP header block exceeds limit")
    end = offset + header_len
    if end > len(data):
        raise WspCodecError("truncated WSP reply headers")
    headers = data[offset:end]
    body = data[end:]
    if len(body) > MAX_BODY_BYTES:
        raise WspCodecError("WSP reply body exceeds limit")
    return WspReply(data[0], status, headers, body)


def encode_reply(pdu: WspReply, *, max_pdu_bytes: int = MAX_PDU_BYTES) -> bytes:
    _validate_tid(pdu.transaction_id)
    if not 0 <= pdu.status <= 255:
        raise WspCodecError("status out of range")
    if len(pdu.headers) > MAX_HEADER_BYTES:
        raise WspCodecError("WSP header block exceeds limit")
    if len(pdu.body) > MAX_BODY_BYTES:
        raise WspCodecError("WSP body exceeds limit")
    data = bytes((pdu.transaction_id, PDU_REPLY, pdu.status))
    data += _write_uintvar(len(pdu.headers)) + pdu.headers + pdu.body
    _check_pdu_size(data, max_pdu_bytes)
    return data


def _validate_tid(transaction_id: int) -> None:
    if not 0 <= transaction_id <= 255:
        raise WspCodecError("transaction id out of range")
