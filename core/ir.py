"""Internal representation (IR) — docs/PROTOCOL.md §3.

The IR is the only structure passed between device adapters, core and service
adapters. It is in-process, bounded, and never carries secret material.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

#: Maximum length of any raw sample recorded in the origin block, so a hostile
#: device cannot exhaust memory while the IR is being built (PROTOCOL §3.3.4).
MAX_RAW_SAMPLE = 512
MAX_HEADER_VALUE_SAMPLE = 64


@dataclass
class Target:
    """Normalized request target (PROTOCOL §3.1); no userinfo, no fragment."""

    scheme: str
    host: str
    port: int
    path: str  # percent-decoded, starts with "/"
    query: str  # percent-decoded, without "?"
    raw_path: str  # safe ASCII form forwarded upstream (percent-encoded)
    raw_query: str  # safe ASCII form forwarded upstream

    @property
    def raw_origin_form(self) -> str:
        url = self.raw_path or "/"
        if self.raw_query:
            url = "%s?%s" % (url, self.raw_query)
        return url


@dataclass
class OriginInfo:
    """What the device *actually* said (PROTOCOL §3.1)."""

    adapter_id: str
    raw_target: str
    raw_headers: List[Tuple[str, str]]
    transport: str
    profile_id: str = ""


@dataclass
class Trace:
    trace_id: str
    seq: int
    started_at: float = field(default_factory=time.time)


@dataclass
class IrRequest:
    method: str
    target: Target
    headers: Dict[str, str]  # lowercased names, policy-sanitized, ordered
    body: bytes
    content_type: Optional[str]
    charset: Optional[str]
    origin: OriginInfo
    trace: Trace


@dataclass
class IrResponse:
    status: int
    headers: Dict[str, str]  # lowercased names, ordered
    body: bytes
    meta: Dict[str, object] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


def truncate_sample(value: str, limit: int = MAX_RAW_SAMPLE) -> str:
    """Bound any device-supplied string recorded into the IR."""
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"
