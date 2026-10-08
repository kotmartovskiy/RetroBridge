"""Ordered transformation pipeline (docs/ARCHITECTURE.md §7, PROTOCOL §5).

Phase 1 wires stages 1 (media-type selection), 3 (charset), 5 (script
stripping) and 7 (size enforcement) plus a tiny gateway-relative ``Location``
rewrite so devices can follow redirects. Each stage is a pure function of the
pipeline state, independently testable; arbitrary modern-web rendering is
explicitly out of scope — content degrades honestly (down-convert or
placeholder), it is never emulated.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from .ir import IrRequest, IrResponse
from .policy import sanitize_response_headers
from .profiles import CapabilityProfile

#: Media types treated as textual even when not listed by the profile; these
#: are down-converted to text rather than passed through as opaque binaries.
TEXTUAL_APPLICATIONS = frozenset(
    {
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/javascript",
        "application/ecmascript",
        "application/rss+xml",
        "application/atom+xml",
        "application/x-www-form-urlencoded",
        "application/ld+json",
        "application/graphql",
    }
)

#: Types whose markup gets script stripping when carried through as markup.
MARKUP_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/vnd.wap.wml",
        "text/wml",
        "text/vnd.wap.wmlc",
    }
)

TRUNCATION_MARKER = "\n[retrobridge: response truncated]\n"
LOCATION_MAX_CHARS = 1024

STAGE_MEDIA_TYPE = "media-type"
STAGE_CHARSET = "charset"
STAGE_SCRIPT_STRIP = "script-strip"
STAGE_SIZE = "size"
STAGE_LOCATION_REWRITE = "location-rewrite"

KNOWN_STAGES = (
    STAGE_MEDIA_TYPE,
    STAGE_CHARSET,
    STAGE_SCRIPT_STRIP,
    STAGE_SIZE,
    STAGE_LOCATION_REWRITE,
)


def parse_content_type(header: str) -> Tuple[str, Optional[str]]:
    """Split ``text/html; charset=utf-8`` into (essence, declared charset)."""
    parts = [part.strip() for part in header.split(";")]
    essence = (parts[0] or "").lower() or "application/octet-stream"
    charset: Optional[str] = None
    for parameter in parts[1:]:
        name, _, value = parameter.partition("=")
        if name.strip().lower() != "charset":
            continue
        charset = value.strip().strip('"').strip("'") or None
    return essence, charset


def _lookup_charset(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    try:
        return codecs.lookup(name.strip()).name
    except (LookupError, ValueError):
        return None


def decode_upstream_text(
    data: bytes, declared_charset: Optional[str]
) -> Tuple[str, Optional[str], List[str]]:
    """PROTOCOL §5.3: declared charset wins; otherwise UTF-8; never crash.

    Returns (text, resolved_charset, notes). Unrepresentable bytes decode to
    U+FFFD (documented substitution, never an exception).
    """
    notes: List[str] = []
    resolved = _lookup_charset(declared_charset)
    if declared_charset and resolved is None:
        notes.append("declared-charset-unsupported:%s" % declared_charset[:32])
    if resolved:
        try:
            text = data.decode(resolved, errors="strict")
        except UnicodeDecodeError:
            text = data.decode(resolved, errors="replace")
            notes.append("decode-replaced:%s" % resolved)
        return text, resolved, notes
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        notes.append("charset-assumed:utf-8")
        return text, "utf-8", notes
    return text, "utf-8", notes


@dataclass
class TransformState:
    """Mutable working state shared by the ordered stages of one response."""

    profile: CapabilityProfile
    status: int
    headers: Dict[str, str]
    body: bytes
    content_type: str = "application/octet-stream"
    charset: Optional[str] = None
    representation: str = "binary"  # "text" | "binary"
    markup: bool = False
    text: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    applied: List[str] = field(default_factory=list)
    truncated: bool = False

    def note(self, value: str) -> None:
        if value not in self.notes:
            self.notes.append(value)


def stage_media_type(state: TransformState) -> None:
    """Stage 1 — choose the device representation class (PROTOCOL §5.1)."""
    essence, declared = parse_content_type(state.headers.get("content-type", ""))
    state.charset = declared
    profile = state.profile
    allowed = {item.lower() for item in profile.content.media_types}
    textual = essence.startswith("text/") or essence in TEXTUAL_APPLICATIONS

    if essence in allowed and not textual:
        state.representation = "binary"
        state.content_type = essence
        return

    if essence in allowed and textual:
        state.representation = "text"
        state.content_type = essence
    elif textual:
        state.representation = "text"
        state.content_type = "text/plain"
        state.note("downgraded:%s" % essence)
    else:
        placeholder = "[retrobridge] unsupported media type: %s\n" % essence
        state.representation = "text"
        state.content_type = "text/plain"
        state.body = placeholder.encode("utf-8")
        state.text = placeholder
        state.note("unsupported-media:%s" % essence)
        return

    state.markup = essence in MARKUP_TYPES
    text, resolved, notes = decode_upstream_text(state.body, declared)
    state.text = text
    state.charset = resolved
    for note in notes:
        state.note(note)


def stage_charset(state: TransformState) -> None:
    """Stage 3 — encode output as the profile's default charset (§5.3)."""
    if state.representation != "text":
        return
    if state.text is None:
        state.text, _, _ = decode_upstream_text(state.body, state.charset)
    target = state.profile.content.default_charset
    state.body = state.text.encode(target, errors="replace")
    state.charset = target
    state.headers["content-type"] = "%s; charset=%s" % (state.content_type, target)
    state.headers.pop("content-length", None)  # recomputed at render time


def _strip_scripts(text: str, markup: bool) -> str:
    """Remove ``<script>`` blocks, inline event handlers and javascript: URLs."""
    if not markup or "<" not in text:
        return text
    from ._html import strip_scripts as _strip  # local import keeps pipeline import cheap

    return _strip(text)


def stage_script_strip(state: TransformState) -> None:
    """Stage 5 — script stripping; the page stays navigable (§5.5)."""
    if state.representation != "text" or not state.markup:
        return
    if state.text is None and state.charset:
        state.text = state.body.decode(state.charset, errors="replace")
    if state.text is None:
        return
    stripped = _strip_scripts(state.text, state.markup)
    if stripped != state.text:
        state.text = stripped
        state.note("scripts-removed")
        target = _lookup_charset(state.profile.content.default_charset) or "iso-8859-1"
        state.body = stripped.encode(target, errors="replace")


def _truncate_text(text: str, cap: int, charset: str, markup: bool) -> Tuple[str, bool]:
    """Cut ``text`` so the encoded bytes fit ``cap``, marker included."""
    marker = TRUNCATION_MARKER
    marker_bytes = len(marker.encode(charset, errors="replace"))
    budget = cap - marker_bytes
    if budget <= 0:
        return "", True
    encoded = text.encode(charset, errors="replace")
    if len(encoded) <= cap:
        return text, False
    cut = text[: min(len(text), budget)]
    if markup and "<" in cut:
        last_open, last_close = cut.rfind("<"), cut.rfind(">")
        if last_open > last_close:
            cut = cut[:last_open]
    while True:
        candidate = cut + marker
        encoded = candidate.encode(charset, errors="replace")
        if len(encoded) <= cap:
            return candidate, True
        # Re-encoding can exceed the byte budget; shave deterministically.
        overshoot = len(encoded) - cap + 16
        if overshoot >= len(cut):
            return marker[:cap] if len(marker.encode(charset, "replace")) <= cap else "", True
        cut = cut[:-overshoot]
        if markup and "<" in cut:
            last_open, last_close = cut.rfind("<"), cut.rfind(">")
            if last_open > last_close:
                cut = cut[:last_open]


def stage_size(state: TransformState) -> None:
    """Stage 7 — enforce ``content.max_body_bytes`` with a visible marker (§5.7)."""
    cap = state.profile.content.max_body_bytes
    if len(state.body) <= cap:
        return
    charset = state.charset or state.profile.content.default_charset
    if state.representation == "binary":
        placeholder = "[retrobridge] response exceeds device limit (%d bytes)\n" % len(state.body)
        state.body = placeholder.encode(charset, errors="replace")[:cap]
        state.content_type = "text/plain"
        state.charset = _lookup_charset(charset) or charset
        state.headers["content-type"] = "%s; charset=%s" % (state.content_type, state.charset)
        state.note("oversize-binary")
        state.truncated = True
        return
    text = state.text
    if text is None:
        text = state.body.decode(charset, errors="replace")
    text, truncated = _truncate_text(text, cap, charset, state.markup)
    if truncated:
        state.text = text
        state.body = text.encode(charset, errors="replace")
        state.note("truncated")
        state.truncated = True


def _gateway_relative_location(location: str, request: IrRequest, gateway_base: str) -> str:
    """Rewrite any upstream Location into an absolute gateway URL (§5.6).

    Redirects are never followed by the gateway itself: the device is sent
    back through the gateway, where egress policy is re-checked on every hop.
    """
    value = location.strip()
    if not value:
        return value
    target = request.target
    default_port = 443 if target.scheme == "https" else 80
    authority = target.host if target.port == default_port else "%s:%d" % (target.host, target.port)
    if "://" in value or value.startswith("//"):
        absolute = value if "://" in value else "%s:%s" % (target.scheme, value)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            return value  # unsupported scheme: leave untouched (policy will deny later)
        netloc = parts.netloc
        path = parts.path or "/"
        query = ("?" + parts.query) if parts.query else ""
        upstream = "%s://%s%s%s" % (parts.scheme, netloc, path, query)
    else:
        base = "%s://%s%s" % (target.scheme, authority, target.raw_origin_form)
        resolved = urljoin(base, value)
        parts = urlsplit(resolved)
        if parts.scheme not in ("http", "https"):
            return value
        netloc = parts.netloc
        path = parts.path or "/"
        query = ("?" + parts.query) if parts.query else ""
        upstream = "%s://%s%s%s" % (parts.scheme, netloc, path, query)
    rewritten = "%s/%s" % (gateway_base.rstrip("/"), upstream)
    if len(rewritten) > LOCATION_MAX_CHARS:
        return value[:LOCATION_MAX_CHARS]
    return rewritten


def stage_location_rewrite(state: TransformState, request: Optional[IrRequest] = None,
                           gateway_base: str = "") -> None:
    """Stage 6 helper — Location → gateway-relative absolute URL (§5.6)."""
    if state.status not in (301, 302, 303, 307, 308) or not gateway_base:
        return
    location = state.headers.get("location")
    if not location or request is None:
        return
    try:
        state.headers["location"] = _gateway_relative_location(location, request, gateway_base)
        state.note("location-rewritten")
    except Exception:  # pragma: no cover - defensive: never fail on a rewrite
        state.headers.pop("location", None)


StageFn = Callable[[TransformState], None]

STAGE_REGISTRY: Dict[str, StageFn] = {
    STAGE_MEDIA_TYPE: stage_media_type,
    STAGE_CHARSET: stage_charset,
    STAGE_SCRIPT_STRIP: stage_script_strip,
    STAGE_SIZE: stage_size,
}


class Pipeline:
    """Named, ordered stages validated at construction time."""

    def __init__(self, stage_names) -> None:
        names = tuple(stage_names)
        if not names:
            raise ValueError("pipeline must declare at least one stage")
        for name in names:
            if name not in KNOWN_STAGES:
                raise ValueError("unknown pipeline stage: %r" % name)
        self.stage_names = names

    @classmethod
    def from_plan(cls, plan) -> "Pipeline":
        return cls(plan)

    def run(
        self,
        response: IrResponse,
        profile: CapabilityProfile,
        request: Optional[IrRequest] = None,
        gateway_base: str = "",
    ) -> IrResponse:
        state = TransformState(
            profile=profile,
            status=response.status,
            headers=sanitize_response_headers(dict(response.headers)),
            body=bytes(response.body),
        )
        if "content-length" in state.headers:
            state.headers.pop("content-length", None)
        for name in self.stage_names:
            if name == STAGE_LOCATION_REWRITE:
                stage_location_rewrite(state, request=request, gateway_base=gateway_base)
            else:
                STAGE_REGISTRY[name](state)
            state.applied.append(name)
        meta = dict(response.meta)
        meta["representation"] = state.representation
        meta["transformed"] = list(state.applied)
        if state.truncated:
            meta["truncated"] = True
        if state.notes:
            meta["transform_notes"] = list(state.notes)
        return IrResponse(
            status=state.status,
            headers=state.headers,
            body=state.body,
            meta=meta,
            errors=list(response.errors),
        )
