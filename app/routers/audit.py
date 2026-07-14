from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models import User
from app.models.audit import AuditEvent
from app.schemas.audit import (
    AuditEventListResponse,
    AuditEventResponse,
    ClientAuditBatchCreate,
)
from app.services.audit import day_bounds, is_user_admin, record_audit
from app.utils.deps import get_current_user

router = APIRouter(tags=["audit"])


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not is_user_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


@router.get("/admin/audit-events", response_model=AuditEventListResponse)
def list_audit_events(
    day: date | None = Query(default=None, description="Calendar day (local server date). Default: today."),
    action: str | None = Query(default=None),
    actor_user_id: UUID | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Admin: user activity for one day (for daily ops review)."""
    target = day or date.today()
    start, end = day_bounds(target)

    q = db.query(AuditEvent).filter(
        AuditEvent.created_at >= start,
        AuditEvent.created_at <= end,
    )
    if action:
        q = q.filter(AuditEvent.action == action)
    if actor_user_id:
        q = q.filter(AuditEvent.actor_user_id == actor_user_id)
    if source:
        q = q.filter(AuditEvent.source == source)

    total = q.count()
    rows = (
        q.order_by(AuditEvent.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return AuditEventListResponse(
        day=target.isoformat(),
        total=total,
        items=[AuditEventResponse.model_validate(r) for r in rows],
    )


@router.get("/admin/audit-events/summary")
def audit_day_summary(
    day: date | None = Query(default=None),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Admin: counts by action for a day."""
    target = day or date.today()
    start, end = day_bounds(target)
    rows = (
        db.query(AuditEvent.action, func.count(AuditEvent.id))
        .filter(AuditEvent.created_at >= start, AuditEvent.created_at <= end)
        .group_by(AuditEvent.action)
        .order_by(func.count(AuditEvent.id).desc())
        .all()
    )
    return {
        "day": target.isoformat(),
        "by_action": [{"action": a, "count": c} for a, c in rows],
        "total": sum(c for _, c in rows),
    }


@router.post("/audit/events", status_code=status.HTTP_201_CREATED)
def post_client_audit_batch(
    body: ClientAuditBatchCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """iOS/console may push light client breadcrumbs (optional)."""
    for item in body.events:
        record_audit(
            db,
            action=item.action if item.action.startswith("client.") else f"client.{item.action}",
            actor=current_user,
            resource_type=item.resource_type,
            resource_id=item.resource_id,
            ledger_id=item.ledger_id,
            summary=item.summary,
            metadata=item.metadata,
            request=request,
        )
    return {"accepted": len(body.events)}
