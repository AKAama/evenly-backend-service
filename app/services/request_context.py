"""Per-request context (IP, client source) for audit without threading Request everywhere."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

from fastapi import Request

_client_ip: ContextVar[str | None] = ContextVar("evenly_client_ip", default=None)
_client_source: ContextVar[str | None] = ContextVar("evenly_client_source", default=None)


def get_request_ip() -> str | None:
    return _client_ip.get()


def get_request_source() -> str | None:
    return _client_source.get()


def bind_request_context(request: Request) -> tuple[Token[Any], Token[Any]]:
    """Set context for the duration of one HTTP request. Returns tokens for reset."""
    from app.services.rate_limit import client_ip
    from app.services.audit import client_source

    ip = client_ip(request)
    if ip in (None, "", "unknown"):
        ip = None
    src = client_source(request)
    if src not in {"ios", "console", "web", "android", "api"}:
        src = "api"

    return (
        _client_ip.set(ip),
        _client_source.set(src),
    )


def reset_request_context(tokens: tuple[Token[Any], Token[Any]] | None) -> None:
    if not tokens:
        return
    ip_token, src_token = tokens
    try:
        _client_ip.reset(ip_token)
    except Exception:
        pass
    try:
        _client_source.reset(src_token)
    except Exception:
        pass
