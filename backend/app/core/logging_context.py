"""Request-scoped trace and session identifiers for application logs."""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar, Token

TRACE_ID_HEADER = "X-Trace-Id"
_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")
_session_id: ContextVar[str] = ContextVar("session_id", default="-")


def resolve_trace_id(value: str | None) -> str:
    candidate = (value or "").strip()
    return candidate if _TRACE_ID_RE.fullmatch(candidate) else uuid.uuid4().hex


def set_trace_id(trace_id: str) -> Token[str]:
    return _trace_id.set(trace_id)


def reset_trace_id(token: Token[str]) -> None:
    _trace_id.reset(token)


def set_session_id(session_id: str) -> Token[str]:
    return _session_id.set(session_id or "-")


def reset_session_id(token: Token[str]) -> None:
    _session_id.reset(token)


class TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id.get()
        record.session_id = _session_id.get()
        return True
