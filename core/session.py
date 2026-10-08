"""Session records for one device connection (ARCHITECTURE §3.2).

A session owns identity (best-effort), transport, the selected profile and
timing from accept to close. It holds no secrets and is safe to log.
"""
from __future__ import annotations

import itertools
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional


def new_trace_id() -> str:
    """Short, unique, non-sensitive correlation id."""
    return uuid.uuid4().hex[:16]


@dataclass
class Session:
    session_id: int
    trace_id: str
    peer: str
    transport: str
    started_at: float = field(default_factory=time.monotonic)
    profile_id: str = ""
    adapter_id: str = ""
    closed: bool = False
    close_reason: str = ""

    @property
    def duration_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)


class SessionManager:
    """Allocates session ids and tracks open sessions (thread-safe, tiny)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = itertools.count(1)
        self._open: Dict[int, Session] = {}

    def open(self, peer: str, transport: str) -> Session:
        session = Session(
            session_id=next(self._counter),
            trace_id=new_trace_id(),
            peer=peer,
            transport=transport,
        )
        with self._lock:
            self._open[session.session_id] = session
        return session

    def close(self, session: Session, reason: str = "completed") -> None:
        session.close_reason = reason
        session.closed = True
        with self._lock:
            self._open.pop(session.session_id, None)

    @property
    def open_count(self) -> int:
        with self._lock:
            return len(self._open)

    def summary(self, session: Session) -> Dict[str, object]:
        """Redacted per-session trace line fields (no header values, no secrets)."""
        return {
            "session": session.session_id,
            "trace": session.trace_id,
            "transport": session.transport,
            "profile": session.profile_id or "-",
            "adapter": session.adapter_id or "-",
            "duration_ms": session.duration_ms,
            "reason": session.close_reason or "-",
        }


def peer_label(address: object) -> str:
    """Best-effort peer string for logs; never includes request data."""
    try:
        host, port = address  # type: ignore[misc]
        return "%s:%s" % (host, port)
    except Exception:
        return str(address)
