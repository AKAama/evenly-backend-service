"""Best-effort audit logging. Failures never break the main request."""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.user import User

logger = logging.getLogger(__name__)


def client_source(request: Request | None) -> str:
    if request is None:
        return "api"
    raw = (request.headers.get("x-client") or request.headers.get("X-Client") or "").strip().lower()
    if raw in {"ios", "console", "web", "android"}:
        return raw
    ua = (request.headers.get("user-agent") or "").lower()
    if "evenly" in ua and "cfnetwork" in ua:
        return "ios"
    if "mozilla" in ua:
        return "web"
    return "api"


def actor_label(user: User | None) -> str | None:
    if user is None:
        return None
    return user.display_name or user.username or user.email


def is_platform_user(user: User | None) -> bool:
    """Ops-only account (console); not a normal app/ledger user."""
    if user is None:
        return False
    return (getattr(user, "account_kind", None) or "app") == "platform"


def is_user_admin(user: User | None) -> bool:
    """Console admin rights: platform accounts only (never regular app users)."""
    return is_platform_user(user)


def user_to_response(user: User) -> "UserResponse":
    from app.schemas.user import UserResponse

    resp = UserResponse.model_validate(user)
    kind = getattr(user, "account_kind", None) or "app"
    return resp.model_copy(
        update={
            "account_kind": kind,
            "is_admin": is_user_admin(user),
        }
    )


def reject_if_platform_for_app(user: User | None) -> None:
    """Block platform ops accounts from ledger/expense membership flows."""
    from fastapi import HTTPException, status

    if is_platform_user(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="平台运营账号不能参与账本业务，请使用控制台管理功能",
        )


def record_audit(
    db: Session | None = None,
    *,
    action: str,
    actor: User | None = None,
    actor_user_id: UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | UUID | None = None,
    ledger_id: UUID | None = None,
    source: str = "api",
    summary: str | None = None,
    metadata: dict[str, Any] | None = None,
    ip: str | None = None,
    request: Request | None = None,
) -> None:
    """Insert one audit row and commit immediately on a sibling session.

    Uses the same DB bind as ``db`` when provided (so tests on in-memory SQLite
    work), otherwise the process default engine. Never relies on the request
    session committing later — that was dropping login/create events.
    """
    from sqlalchemy.orm import sessionmaker

    from app.database import SessionLocal

    # Copy primitives off the request session objects (may expire after commit).
    uid = actor_user_id or (actor.id if actor else None)
    label = actor_label(actor) if actor else None
    rid = str(resource_id) if resource_id is not None else None
    src = client_source(request) if request is not None else (source or "api")
    if src not in {"ios", "console", "web", "android", "api"}:
        src = "api"
    if request is not None and ip is None:
        try:
            from app.services.rate_limit import client_ip

            ip = client_ip(request)
        except Exception:
            ip = None

    if db is not None:
        session = sessionmaker(bind=db.get_bind())()
    else:
        session = SessionLocal()
    try:
        event = AuditEvent(
            actor_user_id=uid,
            actor_label=label,
            action=action,
            resource_type=resource_type,
            resource_id=rid,
            ledger_id=ledger_id,
            source=src,
            summary=(summary or "")[:500] or None,
            metadata_json=metadata,
            ip=ip,
        )
        session.add(event)
        session.commit()
    except Exception:
        logger.exception("audit log failed action=%s", action)
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        session.close()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min)
    end = datetime.combine(day, time.max)
    return start, end
