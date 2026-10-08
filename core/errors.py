"""Internal error model and device-legible rendering (docs/PROTOCOL.md §7).

Every internal error class maps deterministically to one HTTP-ish status and a
tiny plain-text body. Bodies never contain stack traces, filesystem paths,
internal hostnames or secrets; they always carry a trace id for local log
correlation.
"""
from __future__ import annotations

from typing import Dict, Optional, Type

TRACE_LABEL = "trace"


class GatewayError(Exception):
    """Base class for all internal error classes."""

    status: int = 500
    code: str = "Internal"
    default_text: str = "internal gateway error"

    def __init__(self, text: Optional[str] = None, detail: str = "") -> None:
        # ``text`` is device-facing (static, short). ``detail`` is for local
        # structured logs only and must never be rendered to a device.
        message = text if text is not None else self.default_text
        super().__init__(message)
        self.text = message
        self.detail = detail


class MalformedRequest(GatewayError):
    status = 400
    code = "MalformedRequest"
    default_text = "bad request"


class AuthRequiredLocal(GatewayError):
    status = 401
    code = "AuthRequiredLocal"
    default_text = "local authentication required"


class PolicyDenied(GatewayError):
    status = 403
    code = "PolicyDenied"
    default_text = "destination not allowed by gateway policy"


class NotFound(GatewayError):
    status = 404
    code = "NotFound"
    default_text = "not found"


class MethodNotAllowed(GatewayError):
    status = 405
    code = "MethodNotAllowed"
    default_text = "method not allowed"


class UnsupportedByProfile(GatewayError):
    status = 406
    code = "UnsupportedByProfile"
    default_text = "unsupported by device profile"


class RequestTooLarge(GatewayError):
    status = 413
    code = "RequestTooLarge"
    default_text = "request too large"


class RateLimited(GatewayError):
    status = 429
    code = "RateLimited"
    default_text = "gateway rate limit reached"


class UpstreamFailure(GatewayError):
    status = 502
    code = "UpstreamFailure"
    default_text = "upstream request failed"


class UpstreamTimeout(GatewayError):
    status = 504
    code = "UpstreamTimeout"
    default_text = "upstream did not respond"


class HeaderTooLarge(GatewayError):
    status = 431
    code = "HeaderTooLarge"
    default_text = "request header fields too large"


class InternalError(GatewayError):
    status = 500
    code = "Internal"
    default_text = "internal gateway error"


#: Every error class, used by the conformance error-table test.
ERROR_CLASSES: Dict[str, Type[GatewayError]] = {
    cls.code: cls
    for cls in (
        MalformedRequest,
        AuthRequiredLocal,
        PolicyDenied,
        NotFound,
        MethodNotAllowed,
        UnsupportedByProfile,
        RequestTooLarge,
        RateLimited,
        UpstreamFailure,
        UpstreamTimeout,
        HeaderTooLarge,
        InternalError,
    )
}


def render_error(exc: GatewayError, trace_id: str) -> bytes:
    """Render an error as the tiny plain-text body sent to the device."""
    return ("{0}: {1}\n{2}: {3}\n".format(exc.code, exc.text, TRACE_LABEL, trace_id)).encode(
        "ascii", "replace"
    )
