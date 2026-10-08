"""Minimal connectionless WSP GET/Reply adapter.

Transport transaction IDs stay outside IR. Compressed WSP headers and WBXML/WML
are deliberately separate adapter concerns.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.ir import IrRequest, IrResponse, OriginInfo, Trace, truncate_sample
from core.loader import load_device_adapter
from core.profiles import CapabilityProfile
from core.wsp_codec import WspGet, WspReply, decode_get, encode_reply

ADAPTER_ID = "wsp-connectionless/1.0"
TRANSPORT = "wsp-connectionless"

_HTTP_TO_WSP_STATUS = {
    200: 0x20,
    201: 0x21,
    202: 0x22,
    204: 0x24,
    301: 0x30,
    302: 0x32,
    304: 0x34,
    400: 0x40,
    401: 0x41,
    403: 0x43,
    404: 0x44,
    405: 0x45,
    406: 0x46,
    408: 0x48,
    500: 0x50,
    501: 0x51,
    502: 0x52,
    503: 0x53,
    504: 0x54,
}


@dataclass(frozen=True)
class WspRequestContext:
    transaction_id: int
    request: IrRequest


def _http_adapter():
    # Lazy load avoids recursive loader-lock acquisition while this adapter is
    # itself being imported by core.loader.
    return load_device_adapter("http-plain")


def _require_absolute_uri(uri: bytes) -> str:
    text = uri.decode("latin-1").strip()
    if "://" not in text:
        raise ValueError("connectionless WSP adapter requires an absolute URI")
    return text


def get_to_ir(
    pdu: WspGet,
    profile: CapabilityProfile,
    trace_id: str,
    *,
    seq: int = 1,
) -> WspRequestContext:
    """Map bounded WSP GET.req to the existing IR without inventing headers."""
    raw_uri = _require_absolute_uri(pdu.uri)
    target = _http_adapter().parse_request_target(
        raw_uri.encode("latin-1"), profile.default_charset
    )
    return WspRequestContext(
        transaction_id=pdu.transaction_id,
        request=IrRequest(
            method="GET",
            target=target,
            headers={},
            body=b"",
            content_type=None,
            charset=None,
            origin=OriginInfo(
                adapter_id=ADAPTER_ID,
                raw_target=truncate_sample(raw_uri),
                raw_headers=[],
                transport=TRANSPORT,
                profile_id=profile.id,
            ),
            trace=Trace(trace_id=trace_id, seq=seq),
        ),
    )


def decode_get_to_ir(
    data: bytes,
    profile: CapabilityProfile,
    trace_id: str,
    *,
    seq: int = 1,
) -> WspRequestContext:
    return get_to_ir(decode_get(data), profile, trace_id, seq=seq)


def ir_response_to_reply(transaction_id: int, response: IrResponse) -> WspReply:
    """Map an IR response to a WSP Reply envelope.

    Header-field compression is intentionally deferred. HTTP status integers
    are converted to their WSP status octets rather than copied verbatim.
    """
    try:
        status = _HTTP_TO_WSP_STATUS[response.status]
    except KeyError as exc:
        raise ValueError(
            "HTTP status %d has no fixture-backed WSP mapping yet" % response.status
        ) from exc
    return WspReply(
        transaction_id=transaction_id,
        status=status,
        headers=b"",
        body=response.body,
    )


def encode_ir_response(transaction_id: int, response: IrResponse) -> bytes:
    return encode_reply(ir_response_to_reply(transaction_id, response))


def id() -> str:
    return ADAPTER_ID


def transports() -> tuple[str, ...]:
    return (TRANSPORT,)
