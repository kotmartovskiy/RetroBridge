"""Pure routing decision (docs/ARCHITECTURE.md §3.2, §4 step 5).

Given a validated IR request, decide which service adapter handles it and which
transformation plan runs on the way back. No I/O, no sockets, no adapter code
executed here — just data in, data out.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from .errors import PolicyDenied
from .ir import IrRequest
from .policy import normalize_scheme

#: Phase 1 transformation plan (docs/ROADMAP.md): stages 1, 3, 5, 7 plus the
#: tiny Location rewrite needed for devices to follow gateway-relative redirects.
DEFAULT_PLAN: Tuple[str, ...] = (
    "media-type",
    "markup-downconvert",
    "charset",
    "script-strip",
    "size",
    "location-rewrite",
)

#: Which service adapter owns which outbound scheme.
SCHEME_ROUTES: Dict[str, str] = {
    "http": "origin-http",
    "https": "origin-http",
}


@dataclass(frozen=True)
class Route:
    service_adapter_id: str
    plan: Tuple[str, ...] = DEFAULT_PLAN


def route(
    request: IrRequest,
    scheme_routes: Optional[Dict[str, str]] = None,
    plan: Sequence[str] = DEFAULT_PLAN,
) -> Route:
    """Pick the service adapter for ``request``; raises on unroutable targets."""
    routes = SCHEME_ROUTES if scheme_routes is None else scheme_routes
    scheme = normalize_scheme(request.target.scheme)
    adapter_id = routes.get(scheme)
    if not adapter_id:
        raise PolicyDenied(
            "no service adapter for scheme",
            detail="scheme=%r" % scheme,
        )
    return Route(service_adapter_id=adapter_id, plan=tuple(plan))
