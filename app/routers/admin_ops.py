"""Platform admin: global read access to users, ledgers, and bills."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Expense, ExpenseStatus, Ledger, LedgerMember, User
from app.schemas.expense import ExpenseWithDetails, expense_to_with_details
from app.schemas.ledger import (
    LedgerMemberWithUser,
    LedgerOverviewResponse,
    LedgerResponse,
    LedgerWithMembers,
)
from app.schemas.settlement import SettlementInstruction
from app.schemas.user import (
    AdminPasswordReset,
    BadgeCreate,
    BadgeResponse,
    BadgeUpdate,
    UserBadgeUpdate,
    UserResponse,
)
from app.services.audit import is_user_admin, record_audit, user_to_response
from app.services import badges as badge_service
from app.services.auth import set_password
from app.services.settlement import expense_net_amount, expense_scaled_split_amounts
from app.utils.deps import get_current_user, get_ledger_or_404

router = APIRouter(prefix="/admin", tags=["admin-ops"])


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not is_user_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


class AdminUserItem(UserResponse):
    membership_count: int = 0
    expense_created_count: int = 0
    owned_ledger_count: int = 0


class AdminUserListResponse(BaseModel):
    total: int
    items: list[AdminUserItem]


class AdminLedgerItem(LedgerResponse):
    owner_email: str | None = None
    owner_label: str | None = None
    total_spend: float = 0


class AdminLedgerListResponse(BaseModel):
    total: int
    items: list[AdminLedgerItem]


class AdminUserDetailResponse(BaseModel):
    user: UserResponse
    owned_ledgers: list[AdminLedgerItem] = Field(default_factory=list)
    memberships: list[dict] = Field(default_factory=list)


@router.get("/users", response_model=AdminUserListResponse)
def admin_list_users(
    q: str | None = Query(default=None, description="Search email/username/display_name"),
    account_kind: str | None = Query(default=None),
    badge: str | None = Query(
        default=None,
        description="Filter by badge key, or 'none' for users without a badge",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    query = db.query(User)
    if account_kind in {"app", "platform"}:
        query = query.filter(User.account_kind == account_kind)
    if badge is not None and badge.strip():
        key = badge.strip().lower()
        if key in {"none", "null", "empty"}:
            query = query.filter((User.badge.is_(None)) | (User.badge == ""))
        else:
            query = query.filter(User.badge == key)
    if q and q.strip():
        term = f"%{q.strip().lower()}%"
        query = query.filter(
            func.lower(User.email).like(term)
            | func.lower(User.username).like(term)
            | func.lower(func.coalesce(User.display_name, "")).like(term)
        )
    total = query.count()
    users = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()

    items: list[AdminUserItem] = []
    for u in users:
        base = user_to_response(u)
        membership_count = (
            db.query(func.count(LedgerMember.id))
            .filter(LedgerMember.user_id == u.id, LedgerMember.status == "active")
            .scalar()
            or 0
        )
        expense_created_count = (
            db.query(func.count(Expense.id)).filter(Expense.created_by == u.id).scalar() or 0
        )
        owned_ledger_count = (
            db.query(func.count(Ledger.id)).filter(Ledger.owner_id == u.id).scalar() or 0
        )
        items.append(
            AdminUserItem(
                **base.model_dump(),
                membership_count=int(membership_count),
                expense_created_count=int(expense_created_count),
                owned_ledger_count=int(owned_ledger_count),
            )
        )
    return AdminUserListResponse(total=total, items=items)


@router.get("/users/{user_id}", response_model=AdminUserDetailResponse)
def admin_get_user(
    user_id: UUID,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    owned = (
        db.query(Ledger)
        .filter(Ledger.owner_id == user_id)
        .order_by(Ledger.created_at.desc())
        .all()
    )
    memberships = (
        db.query(LedgerMember)
        .options(joinedload(LedgerMember.ledger))
        .filter(LedgerMember.user_id == user_id)
        .order_by(LedgerMember.created_at.desc())
        .all()
    )
    return AdminUserDetailResponse(
        user=user_to_response(user),
        owned_ledgers=[_ledger_item(db, ledger) for ledger in owned],
        memberships=[
            {
                "member_id": str(m.id),
                "ledger_id": str(m.ledger_id),
                "ledger_name": m.ledger.name if m.ledger else None,
                "status": m.status,
                "joined_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in memberships
        ],
    )


def _badge_item(db: Session, row, counts: dict) -> BadgeResponse:
    return BadgeResponse(
        id=row.id,
        key=row.key,
        label=row.label,
        description=row.description,
        color=row.color or "blue",
        sort_order=row.sort_order or 0,
        is_active=bool(row.is_active),
        user_count=int(counts.get(row.key, 0) or 0),
        created_at=row.created_at,
    )


@router.get("/badges")
def admin_list_badges(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """All nameplate definitions (including inactive) + holder counts."""
    counts = dict(
        db.query(User.badge, func.count(User.id))
        .filter(User.badge.isnot(None), User.badge != "")
        .group_by(User.badge)
        .all()
    )
    rows = badge_service.list_badges(db, active_only=False)
    return {
        "items": [_badge_item(db, r, counts) for r in rows],
        "unassigned_count": int(
            db.query(func.count(User.id))
            .filter((User.badge.is_(None)) | (User.badge == ""))
            .scalar()
            or 0
        ),
    }


@router.post("/badges", response_model=BadgeResponse, status_code=status.HTTP_201_CREATED)
def admin_create_badge(
    payload: BadgeCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    x_client: str | None = Header(default=None, alias="X-Client"),
):
    try:
        row = badge_service.create_badge(
            db,
            label=payload.label,
            description=payload.description,
            color=payload.color,
            key=payload.key,
            sort_order=payload.sort_order,
        )
        db.commit()
        db.refresh(row)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    source = x_client.strip().lower() if isinstance(x_client, str) and x_client.strip() else "console"
    record_audit(
        db,
        action="badge.create",
        actor=admin,
        resource_type="badge",
        resource_id=row.id,
        summary=f"创建铭牌「{row.label}」",
        metadata={"key": row.key},
        source=source,
    )
    return _badge_item(db, row, {})


@router.patch("/badges/{badge_id}", response_model=BadgeResponse)
def admin_update_badge(
    badge_id: UUID,
    payload: BadgeUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    x_client: str | None = Header(default=None, alias="X-Client"),
):
    data = payload.model_dump(exclude_unset=True)
    try:
        row = badge_service.update_badge(
            db,
            badge_id,
            label=data.get("label"),
            description=data["description"] if "description" in data else ...,
            color=data.get("color"),
            sort_order=data.get("sort_order"),
            is_active=data.get("is_active"),
        )
        db.commit()
        db.refresh(row)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    counts = dict(
        db.query(User.badge, func.count(User.id))
        .filter(User.badge == row.key)
        .group_by(User.badge)
        .all()
    )
    source = x_client.strip().lower() if isinstance(x_client, str) and x_client.strip() else "console"
    record_audit(
        db,
        action="badge.update",
        actor=admin,
        resource_type="badge",
        resource_id=row.id,
        summary=f"更新铭牌「{row.label}」",
        metadata=data,
        source=source,
    )
    return _badge_item(db, row, counts)


@router.delete("/badges/{badge_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_badge(
    badge_id: UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    x_client: str | None = Header(default=None, alias="X-Client"),
):
    try:
        key = badge_service.delete_badge(db, badge_id, user_model=User)
        db.commit()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    source = x_client.strip().lower() if isinstance(x_client, str) and x_client.strip() else "console"
    record_audit(
        db,
        action="badge.delete",
        actor=admin,
        resource_type="badge",
        resource_id=badge_id,
        summary=f"删除铭牌 {key}",
        metadata={"key": key},
        source=source,
    )
    return None


@router.patch("/users/{user_id}/badge", response_model=UserResponse)
def admin_set_user_badge(
    user_id: UUID,
    payload: UserBadgeUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    x_client: str | None = Header(default=None, alias="X-Client"),
):
    """Assign or clear a display nameplate (platform admin only)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        key = badge_service.normalize_badge(db, payload.badge)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    user.badge = key
    db.commit()
    db.refresh(user)

    source = x_client.strip().lower() if isinstance(x_client, str) and x_client.strip() else "console"
    record_audit(
        db,
        action="user.badge_set",
        actor=admin,
        resource_type="user",
        resource_id=user.id,
        summary=f"设置铭牌 {user.username} → {key or '（清除）'}",
        metadata={"badge": key},
        source=source,
    )
    return user_to_response(user, db)


@router.post("/users/{user_id}/reset-password")
def admin_reset_user_password(
    user_id: UUID,
    payload: AdminPasswordReset,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    x_client: str | None = Header(default=None, alias="X-Client"),
):
    """Set a new password for any user (app or platform). Does not email the user."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    new_password = (payload.new_password or "").strip()
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    if len(new_password) > 128:
        raise HTTPException(status_code=400, detail="新密码过长")

    set_password(db, user, new_password)
    db.commit()

    source = x_client.strip().lower() if isinstance(x_client, str) and x_client.strip() else "console"
    record_audit(
        db,
        action="user.password_reset_admin",
        actor=admin,
        resource_type="user",
        resource_id=user.id,
        summary=f"管理员重置密码 {user.username}",
        metadata={"target_username": user.username},
        source=source,
    )
    return {
        "message": "密码已重置",
        "user_id": str(user.id),
        "username": user.username,
    }


@router.get("/ledgers", response_model=AdminLedgerListResponse)
def admin_list_ledgers(
    q: str | None = Query(default=None, description="Search ledger name"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    query = db.query(Ledger)
    if q and q.strip():
        query = query.filter(Ledger.name.ilike(f"%{q.strip()}%"))
    total = query.count()
    ledgers = query.order_by(Ledger.created_at.desc()).offset(offset).limit(limit).all()
    return AdminLedgerListResponse(
        total=total,
        items=[_ledger_item(db, ledger) for ledger in ledgers],
    )


@router.get("/ledgers/{ledger_id}/overview", response_model=LedgerOverviewResponse)
def admin_ledger_overview(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Full ledger payload for ops (no membership required)."""
    from app.routers.expenses import required_confirmation_user_ids

    ledger = get_ledger_or_404(db, ledger_id)

    members = (
        db.query(LedgerMember)
        .options(joinedload(LedgerMember.user))
        .filter(LedgerMember.ledger_id == ledger_id)
        .all()
    )
    active_members = [m for m in members if m.status == "active"]
    member_responses = [
        LedgerMemberWithUser(
            id=member.id,
            user_id=member.user_id,
            nickname=member.nickname,
            joined_at=member.joined_at,
            user=user_to_response(member.user) if member.user else None,
            is_temporary=member.is_temporary,
            temporary_name=member.temporary_name,
            status=member.status,
        )
        for member in members
    ]

    expenses = (
        db.query(Expense)
        .options(
            joinedload(Expense.payer),
            joinedload(Expense.splits),
            joinedload(Expense.confirmations),
        )
        .filter(Expense.ledger_id == ledger_id)
        .order_by(Expense.created_at.desc())
        .all()
    )
    expense_responses: list[ExpenseWithDetails] = []
    for expense in expenses:
        effective_status = expense.status
        if effective_status == ExpenseStatus.PENDING:
            split_ids = {s.user_id for s in expense.splits if s.user_id is not None}
            required_ids = required_confirmation_user_ids(
                split_ids,
                created_by=expense.created_by,
                payer_id=expense.payer_id,
            )
            confirmed_ids = {
                c.user_id for c in expense.confirmations if c.status == "confirmed"
            }
            if required_ids <= confirmed_ids:
                effective_status = ExpenseStatus.CONFIRMED
        expense_responses.append(
            expense_to_with_details(expense, status=effective_status.value)
        )

    from app.services.settlement import SettlementCalculator

    calculator = SettlementCalculator(db, ledger_id)
    suggestions = [
        SettlementInstruction(
            from_user_id=s["from_user_id"],
            from_user_name=s["from_user_name"],
            to_user_id=s["to_user_id"],
            to_user_name=s["to_user_name"],
            amount=s["amount"],
            includes_unconfirmed=bool(s.get("includes_unconfirmed", False)),
        )
        for s in calculator.calculate_settlements()
    ]

    ledger_response = LedgerWithMembers.model_validate(ledger)
    ledger_response.members = member_responses
    ledger_response.member_count = len(active_members)
    ledger_response.expense_count = len(expenses)
    return LedgerOverviewResponse(
        ledger=ledger_response,
        expenses=expense_responses,
        settlement_suggestions=suggestions,
        settlement_history=[],
    )


def _ledger_item(db: Session, ledger: Ledger) -> AdminLedgerItem:
    owner = db.query(User).filter(User.id == ledger.owner_id).first()
    member_count = (
        db.query(func.count(LedgerMember.id))
        .filter(LedgerMember.ledger_id == ledger.id, LedgerMember.status == "active")
        .scalar()
        or 0
    )
    expenses = (
        db.query(Expense)
        .filter(Expense.ledger_id == ledger.id, Expense.status != ExpenseStatus.REJECTED)
        .all()
    )
    base = LedgerResponse.model_validate(ledger)
    data = base.model_dump()
    data.update(
        {
            "member_count": int(member_count),
            "expense_count": len(expenses),
            "owner_email": owner.email if owner else None,
            "owner_label": (owner.display_name or owner.username) if owner else None,
            "total_spend": float(sum(expense_net_amount(e) for e in expenses)),
        }
    )
    return AdminLedgerItem(**data)
