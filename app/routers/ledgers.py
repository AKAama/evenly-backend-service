import logging
import secrets
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile, status
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import joinedload, Session
from typing import List

from app.config import settings
from app.database import get_db
from app.models import (
    User,
    Ledger,
    LedgerInviteLink,
    LedgerMember,
    Expense,
    ExpenseConfirmation,
    ExpenseSplit,
    ExpenseStatus,
    Settlement,
)
from app.schemas.ledger import (
    LedgerCreate,
    LedgerUpdate,
    LedgerResponse,
    LedgerWithMembers,
    AddMemberRequest,
    MemberResponse,
    MemberCreate,
    LedgerInvitationResponse,
    LedgerInviteLinkResponse,
    LedgerInvitePreviewResponse,
    JoinLedgerResponse,
    LedgerMemberWithUser,
    LedgerOverviewResponse,
)
from app.schemas.expense import expense_to_with_details
from app.schemas.settlement import SettlementInstruction
from app.schemas.user import UserResponse
from app.services.audit import user_to_response
from app.utils.deps import get_current_user, get_ledger_or_404, require_ledger_member
from app.services.push import PushEvent, build_payload, send_push_safely
from app.services import invitation_cache


def _x_client_source(x_client) -> str:
    if isinstance(x_client, str) and x_client.strip():
        return x_client.strip().lower()
    return "api"

router = APIRouter(prefix="/ledgers", tags=["ledgers"])
logger = logging.getLogger(__name__)


def _public_join_url(token: str) -> str:
    base = (settings.public_app_base_url or "https://app.ismyh.cn").rstrip("/")
    return f"{base}/join/{token}"


def _new_invite_token() -> str:
    # URL-safe, short enough for QR density, long enough to be unguessable.
    return secrets.token_urlsafe(12)


def _active_invite_link(db: Session, ledger_id: UUID) -> LedgerInviteLink | None:
    return (
        db.query(LedgerInviteLink)
        .filter(
            LedgerInviteLink.ledger_id == ledger_id,
            LedgerInviteLink.revoked_at.is_(None),
        )
        .order_by(LedgerInviteLink.created_at.desc())
        .first()
    )


def _require_ledger_owner(ledger: Ledger, current_user: User) -> None:
    if ledger.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only owner can manage invite links")


def _invite_link_response(link: LedgerInviteLink, ledger: Ledger) -> LedgerInviteLinkResponse:
    return LedgerInviteLinkResponse(
        token=link.token,
        url=_public_join_url(link.token),
        ledger_id=ledger.id,
        ledger_name=ledger.name,
        created_at=link.created_at,
    )


def _get_active_link_by_token(db: Session, token: str) -> tuple[LedgerInviteLink, Ledger]:
    link = (
        db.query(LedgerInviteLink)
        .options(joinedload(LedgerInviteLink.ledger).joinedload(Ledger.owner))
        .filter(
            LedgerInviteLink.token == token,
            LedgerInviteLink.revoked_at.is_(None),
        )
        .first()
    )
    if link is None or link.ledger is None:
        raise HTTPException(status_code=404, detail="Invite link is invalid or expired")
    return link, link.ledger


@router.post("", response_model=LedgerWithMembers, status_code=status.HTTP_201_CREATED)
def create_ledger(
    ledger: LedgerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_client: str | None = Header(default=None, alias="X-Client"),
):
    """Create a new ledger and add the creator as owner member"""
    from app.services.audit import reject_if_platform_for_app

    reject_if_platform_for_app(current_user)
    logger.info(
        "创建账本 name=%s owner_id=%s 初始成员数=%d",
        ledger.name,
        current_user.id,
        len(ledger.members),
    )
    
    # Check if ledger name already exists for this user
    existing = db.query(Ledger).filter(
        Ledger.name == ledger.name,
        Ledger.owner_id == current_user.id
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail="账本名称已存在，请使用其他名称")
    
    db_ledger = Ledger(
        name=ledger.name,
        owner_id=current_user.id,
        currency=ledger.currency,
        require_confirmation=ledger.require_confirmation,
    )
    db.add(db_ledger)
    db.commit()
    db.refresh(db_ledger)

    # Add creator as a member
    owner_member = LedgerMember(
        ledger_id=db_ledger.id,
        user_id=current_user.id,
        status="active",
    )
    db.add(owner_member)
    db.flush()
    db.refresh(owner_member)

    # Add initial members from request (if any)
    member_responses = []
    invited_user_ids: list[UUID] = []
    for member_data in ledger.members:
        # Handle temporary members
        if member_data.is_temporary:
            if not member_data.temporary_name:
                continue
            member = LedgerMember(
                ledger_id=db_ledger.id,
                user_id=None,
                nickname=member_data.temporary_name,
                is_temporary=True,
                temporary_name=member_data.temporary_name,
                status="active",
            )
        else:
            # Regular member - must have user_id
            if not member_data.user_id:
                continue
            # Check if user exists
            user = db.query(User).filter(User.id == member_data.user_id).first()
            if not user:
                continue
            member = LedgerMember(
                ledger_id=db_ledger.id,
                user_id=member_data.user_id,
                status="pending",
            )
            invited_user_ids.append(member_data.user_id)
        
        db.add(member)
        db.commit()
        db.refresh(member)
        
        # Build member response
        member_user = db.query(User).filter(User.id == member.user_id).first() if member.user_id and not member.is_temporary else None
        member_responses.append(MemberResponse(
            id=member.id,
            user_id=member.user_id,
            nickname=member.nickname,
            joined_at=member.joined_at,
            user=user_to_response(member_user) if member_user else None,
            is_temporary=member.is_temporary,
                temporary_name=member.temporary_name,
            status=member.status,
        ))

    db.commit()

    if invited_user_ids:
        invitation_cache.invalidate_pending_invitations_many(invited_user_ids)
        send_push_safely(db, invited_user_ids, build_payload(
            event=PushEvent.LEDGER_INVITED,
            actor_name=current_user.display_name or current_user.username,
            ledger_name=db_ledger.name,
            ledger_id=str(db_ledger.id),
        ))

    # Build response with all members including owner
    owner_user_response = user_to_response(current_user)
    owner_member_response = MemberResponse(
        id=owner_member.id,
        user_id=current_user.id,
        joined_at=owner_member.joined_at,
        user=owner_user_response,
        is_temporary=False,
        temporary_name=None
    )
    
    all_members = [owner_member_response] + member_responses
    
    response = LedgerWithMembers(
        id=db_ledger.id,
        name=db_ledger.name,
        owner_id=db_ledger.owner_id,
        currency=db_ledger.currency,
        require_confirmation=bool(db_ledger.require_confirmation),
        created_at=db_ledger.created_at,
        updated_at=db_ledger.updated_at,
        members=all_members
    )

    from app.services.audit import record_audit

    record_audit(
        db,
        action="ledger.create",
        actor=current_user,
        resource_type="ledger",
        resource_id=db_ledger.id,
        ledger_id=db_ledger.id,
        summary=f"创建账本「{db_ledger.name}」",
        source=_x_client_source(x_client),
    )
    
    return response


@router.get("", response_model=List[LedgerResponse])
def get_ledgers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all ledgers the current user is a member of"""
    member_query = db.query(LedgerMember).filter(
        LedgerMember.user_id == current_user.id,
        LedgerMember.status == "active",
    )
    member_ledger_ids = [m.ledger_id for m in member_query.all()]

    member_count = (
        db.query(func.count(LedgerMember.id))
        .filter(LedgerMember.ledger_id == Ledger.id, LedgerMember.status == "active")
        .correlate(Ledger)
        .scalar_subquery()
    )
    expense_count = (
        db.query(func.count(Expense.id))
        .filter(Expense.ledger_id == Ledger.id)
        .correlate(Ledger)
        .scalar_subquery()
    )
    rows = (
        db.query(
            Ledger,
            member_count.label("member_count"),
            expense_count.label("expense_count"),
        )
        .filter(
            Ledger.id.in_(member_ledger_ids),
            # Archived (orphan) ledgers are not shown in the app; platform manages them.
            (Ledger.status == "active") | (Ledger.status.is_(None)),
        )
        .all()
    )

    return [
        LedgerResponse.model_validate(ledger).model_copy(update={
            "member_count": row_member_count,
            "expense_count": row_expense_count,
        })
        for ledger, row_member_count, row_expense_count in rows
    ]


@router.get("/invitations/pending", response_model=List[LedgerInvitationResponse])
def get_pending_invitations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cached = invitation_cache.get_pending_invitations(current_user.id)
    if cached is not None:
        return [LedgerInvitationResponse.model_validate(item) for item in cached]

    rows = (
        db.query(LedgerMember, Ledger, User)
        .join(Ledger, Ledger.id == LedgerMember.ledger_id)
        .join(User, User.id == Ledger.owner_id)
        .filter(
            LedgerMember.user_id == current_user.id,
            LedgerMember.status == "pending",
        )
        .all()
    )
    invitations = [
        LedgerInvitationResponse(
            id=membership.id,
            ledger_id=ledger.id,
            ledger_name=ledger.name,
            invited_by_name=inviter.display_name,
            created_at=membership.created_at,
        )
        for membership, ledger, inviter in rows
    ]
    invitation_cache.set_pending_invitations(
        current_user.id,
        [item.model_dump(mode="json") for item in invitations],
    )
    return invitations


@router.post("/invitations/{invitation_id}/accept", status_code=status.HTTP_204_NO_CONTENT)
def accept_invitation(
    invitation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    invitation = db.query(LedgerMember).filter(
        LedgerMember.id == invitation_id,
        LedgerMember.user_id == current_user.id,
        LedgerMember.status == "pending",
    ).first()
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")
    invitation.status = "active"
    db.commit()
    invitation_cache.invalidate_pending_invitations(current_user.id)


@router.post("/invitations/{invitation_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
def reject_invitation(
    invitation_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reject a pending invitation while retaining the membership row for history/re-invite."""
    invitation = db.query(LedgerMember).filter(
        LedgerMember.id == invitation_id,
        LedgerMember.user_id == current_user.id,
        LedgerMember.status == "pending",
    ).first()
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")
    invitation.status = "rejected"
    db.commit()
    invitation_cache.invalidate_pending_invitations(current_user.id)


# --- QR / Universal Link invites (paths registered before /{ledger_id}) ---


@router.get("/invite-links/{token}/preview", response_model=LedgerInvitePreviewResponse)
def preview_invite_link(token: str, db: Session = Depends(get_db)):
    """Public preview for landing page (no auth). Does not join the ledger."""
    link, ledger = _get_active_link_by_token(db, token)
    owner = ledger.owner
    owner_name = (
        (owner.display_name or owner.username or owner.email)
        if owner is not None
        else "账本主人"
    )
    return LedgerInvitePreviewResponse(
        token=link.token,
        ledger_id=ledger.id,
        ledger_name=ledger.name,
        owner_name=owner_name,
        valid=True,
    )


@router.post("/invite-links/{token}/join", response_model=JoinLedgerResponse)
def join_via_invite_link(
    token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_client: str | None = Header(default=None, alias="X-Client"),
):
    """Join a ledger via QR / Universal Link. Authenticated user becomes an active member."""
    from app.services.audit import reject_if_platform_for_app

    reject_if_platform_for_app(current_user)
    link, ledger = _get_active_link_by_token(db, token)

    existing = (
        db.query(LedgerMember)
        .filter(
            LedgerMember.ledger_id == ledger.id,
            LedgerMember.user_id == current_user.id,
        )
        .first()
    )
    if existing is not None:
        if existing.status == "active":
            return JoinLedgerResponse(
                ledger_id=ledger.id,
                ledger_name=ledger.name,
                status="already_member",
                member_id=existing.id,
            )
        # pending / rejected / removed → activate immediately (open QR invite).
        existing.status = "active"
        db.commit()
        db.refresh(existing)
        invitation_cache.invalidate_pending_invitations(current_user.id)
        from app.services.audit import record_audit

        record_audit(
            db,
            action="ledger.join_invite",
            actor=current_user,
            resource_type="ledger",
            resource_id=ledger.id,
            ledger_id=ledger.id,
            summary=f"通过邀请链接加入「{ledger.name}」",
            source=_x_client_source(x_client),
        )
        return JoinLedgerResponse(
            ledger_id=ledger.id,
            ledger_name=ledger.name,
            status="active",
            member_id=existing.id,
        )

    member = LedgerMember(
        ledger_id=ledger.id,
        user_id=current_user.id,
        status="active",
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    invitation_cache.invalidate_pending_invitations(current_user.id)
    logger.info(
        "通过邀请链接加入账本 ledger_id=%s user_id=%s token=%s…",
        ledger.id,
        current_user.id,
        token[:6],
    )
    from app.services.audit import record_audit

    record_audit(
        db,
        action="ledger.join_invite",
        actor=current_user,
        resource_type="ledger",
        resource_id=ledger.id,
        ledger_id=ledger.id,
        summary=f"通过邀请链接加入「{ledger.name}」",
        source=_x_client_source(x_client),
    )
    return JoinLedgerResponse(
        ledger_id=ledger.id,
        ledger_name=ledger.name,
        status="active",
        member_id=member.id,
    )


@router.get("/{ledger_id}/invite-link", response_model=LedgerInviteLinkResponse)
def get_or_create_invite_link(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Owner: return the active share/QR invite link, creating one if needed."""
    ledger = get_ledger_or_404(db, ledger_id)
    _require_ledger_owner(ledger, current_user)

    link = _active_invite_link(db, ledger_id)
    if link is None:
        link = LedgerInviteLink(
            ledger_id=ledger_id,
            token=_new_invite_token(),
            created_by=current_user.id,
        )
        db.add(link)
        db.commit()
        db.refresh(link)
    return _invite_link_response(link, ledger)


@router.post("/{ledger_id}/invite-link/rotate", response_model=LedgerInviteLinkResponse)
def rotate_invite_link(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Owner: revoke the current invite link and issue a new one (invalidates old QR)."""
    ledger = get_ledger_or_404(db, ledger_id)
    _require_ledger_owner(ledger, current_user)

    now = datetime.utcnow()
    for link in (
        db.query(LedgerInviteLink)
        .filter(
            LedgerInviteLink.ledger_id == ledger_id,
            LedgerInviteLink.revoked_at.is_(None),
        )
        .all()
    ):
        link.revoked_at = now

    new_link = LedgerInviteLink(
        ledger_id=ledger_id,
        token=_new_invite_token(),
        created_by=current_user.id,
    )
    db.add(new_link)
    db.commit()
    db.refresh(new_link)
    return _invite_link_response(new_link, ledger)


@router.get("/{ledger_id}", response_model=LedgerWithMembers)
def get_ledger(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get ledger details with members"""
    ledger = get_ledger_or_404(db, ledger_id)
    require_ledger_member(db, ledger_id, current_user)

    # Include pending/rejected so owners can see outstanding and declined invitations.
    members = (
        db.query(LedgerMember)
        .options(joinedload(LedgerMember.user))
        .filter(LedgerMember.ledger_id == ledger_id)
        .all()
    )
    member_responses: list[LedgerMemberWithUser] = []
    for m in members:
        user = m.user if m.user_id and not m.is_temporary else None
        member_responses.append(LedgerMemberWithUser(
            id=m.id,
            user_id=m.user_id,
            nickname=m.nickname,
            joined_at=m.joined_at,
            user=user_to_response(user) if user else None,
            is_temporary=m.is_temporary,
            temporary_name=m.temporary_name,
            status=m.status,
        ))

    response = LedgerWithMembers.model_validate(ledger)
    response.members = member_responses
    return response


@router.get("/{ledger_id}/overview", response_model=LedgerOverviewResponse)
def get_ledger_overview(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Load all data required by the main ledger screen in one request."""
    ledger = (
        db.query(Ledger)
        .join(
            LedgerMember,
            and_(
                LedgerMember.ledger_id == Ledger.id,
                LedgerMember.user_id == current_user.id,
                LedgerMember.user_id.is_not(None),
                LedgerMember.status == "active",
            ),
        )
        .filter(Ledger.id == ledger_id)
        .first()
    )
    if ledger is None:
        raise HTTPException(status_code=404, detail="Ledger not found")

    members = (
        db.query(LedgerMember)
        .options(joinedload(LedgerMember.user))
        .filter(LedgerMember.ledger_id == ledger_id)
        .all()
    )
    active_members = [member for member in members if member.status == "active"]
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
    expense_responses = []
    for expense in expenses:
        effective_status = expense.status
        if effective_status == ExpenseStatus.PENDING:
            from app.routers.expenses import required_confirmation_user_ids

            split_ids = {
                split.user_id for split in expense.splits if split.user_id is not None
            }
            required_ids = required_confirmation_user_ids(
                split_ids,
                created_by=expense.created_by,
                payer_id=expense.payer_id,
            )
            confirmed_ids = {
                confirmation.user_id
                for confirmation in expense.confirmations
                if confirmation.status == "confirmed"
            }
            if required_ids <= confirmed_ids:
                effective_status = ExpenseStatus.CONFIRMED

        expense_responses.append(
            expense_to_with_details(
                expense,
                status=effective_status.value,
            )
        )

    history = []

    # Same projected flow as /settlements: confirmed + pending, with unconfirmed flags.
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
        settlement_history=history,
    )


@router.patch("/{ledger_id}", response_model=LedgerWithMembers)
def update_ledger(
    ledger_id: UUID,
    payload: LedgerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_client: str | None = Header(default=None, alias="X-Client"),
):
    """Update ledger settings (owner only). Turning off confirmation auto-confirms pending bills."""
    from app.services.audit import record_audit

    ledger = get_ledger_or_404(db, ledger_id)
    if ledger.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only owner can update the ledger")

    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "name" in data and data["name"] is not None:
        new_name = data["name"].strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="账本名称不能为空")
        existing = (
            db.query(Ledger)
            .filter(
                Ledger.name == new_name,
                Ledger.owner_id == current_user.id,
                Ledger.id != ledger_id,
            )
            .first()
        )
        if existing:
            raise HTTPException(status_code=400, detail="账本名称已存在，请使用其他名称")
        ledger.name = new_name

    if "currency" in data and data["currency"] is not None:
        ledger.currency = data["currency"]

    turned_off_confirmation = False
    if "require_confirmation" in data and data["require_confirmation"] is not None:
        new_flag = bool(data["require_confirmation"])
        if ledger.require_confirmation and not new_flag:
            turned_off_confirmation = True
        ledger.require_confirmation = new_flag

    if turned_off_confirmation:
        # Pending bills should immediately enter settlement when confirmation is disabled.
        (
            db.query(Expense)
            .filter(
                Expense.ledger_id == ledger_id,
                Expense.status == ExpenseStatus.PENDING,
            )
            .update({"status": ExpenseStatus.CONFIRMED}, synchronize_session=False)
        )

    ledger.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(ledger)

    members = (
        db.query(LedgerMember)
        .options(joinedload(LedgerMember.user))
        .filter(LedgerMember.ledger_id == ledger_id)
        .all()
    )
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
    active_count = sum(1 for m in members if m.status == "active")

    record_audit(
        db,
        action="ledger.update",
        actor=current_user,
        resource_type="ledger",
        resource_id=ledger.id,
        ledger_id=ledger.id,
        summary=f"更新账本「{ledger.name}」设置",
        metadata=data,
        source=_x_client_source(x_client),
    )

    response = LedgerWithMembers.model_validate(ledger)
    response.members = member_responses
    response.member_count = active_count
    return response


@router.post("/{ledger_id}/cover", response_model=LedgerWithMembers)
async def upload_ledger_cover(
    ledger_id: UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_client: str | None = Header(default=None, alias="X-Client"),
):
    """Upload a custom bookshelf cover (owner only). Same COS pipeline as avatars."""
    from app.config import settings
    from app.services.audit import record_audit
    from app.services.cos import get_cos_service

    ledger = get_ledger_or_404(db, ledger_id)
    if ledger.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only owner can set the ledger cover")

    if not settings.cos:
        raise HTTPException(status_code=503, detail="图片上传未配置")

    allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="仅支持 jpeg / png / gif / webp")

    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片不能超过 5MB")

    try:
        cos_service = get_cos_service()
        if cos_service is None:
            raise RuntimeError("COS service unavailable")
        cover_url = cos_service.upload_file(
            file_data=contents,
            filename=file.filename or "cover.jpg",
            folder="ledger-covers",
        )
    except Exception as exc:
        logger.exception("账本封面上传失败 ledger_id=%s: %s", ledger_id, exc)
        raise HTTPException(status_code=502, detail="封面上传暂时不可用") from exc

    old_url = ledger.cover_url
    ledger.cover_url = cover_url
    ledger.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(ledger)

    if old_url and old_url != cover_url:
        try:
            cos_service.delete_file(old_url)
        except Exception:
            logger.warning("删除旧账本封面失败 url=%s", old_url)

    record_audit(
        db,
        action="ledger.cover_upload",
        actor=current_user,
        resource_type="ledger",
        resource_id=ledger.id,
        ledger_id=ledger.id,
        summary=f"更新账本「{ledger.name}」封面",
        source=_x_client_source(x_client),
    )

    members = (
        db.query(LedgerMember)
        .options(joinedload(LedgerMember.user))
        .filter(LedgerMember.ledger_id == ledger_id)
        .all()
    )
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
    response = LedgerWithMembers.model_validate(ledger)
    response.members = member_responses
    response.member_count = sum(1 for m in members if m.status == "active")
    return response


@router.delete("/{ledger_id}/cover", response_model=LedgerWithMembers)
def delete_ledger_cover(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    x_client: str | None = Header(default=None, alias="X-Client"),
):
    """Remove custom cover and fall back to generated book style (owner only)."""
    from app.services.audit import record_audit
    from app.services.cos import get_cos_service

    ledger = get_ledger_or_404(db, ledger_id)
    if ledger.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only owner can clear the ledger cover")

    old_url = ledger.cover_url
    ledger.cover_url = None
    ledger.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(ledger)

    if old_url:
        try:
            cos = get_cos_service()
            if cos is not None:
                cos.delete_file(old_url)
        except Exception:
            logger.warning("从 COS 删除账本封面失败 url=%s", old_url)

    record_audit(
        db,
        action="ledger.cover_delete",
        actor=current_user,
        resource_type="ledger",
        resource_id=ledger.id,
        ledger_id=ledger.id,
        summary=f"移除账本「{ledger.name}」封面",
        source=_x_client_source(x_client),
    )

    members = (
        db.query(LedgerMember)
        .options(joinedload(LedgerMember.user))
        .filter(LedgerMember.ledger_id == ledger_id)
        .all()
    )
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
    response = LedgerWithMembers.model_validate(ledger)
    response.members = member_responses
    response.member_count = sum(1 for m in members if m.status == "active")
    return response


@router.delete("/{ledger_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ledger(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a ledger (only owner can delete)"""
    ledger = get_ledger_or_404(db, ledger_id)

    if ledger.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only owner can delete the ledger")

    db.delete(ledger)
    db.commit()
    return None


# Member management endpoints

@router.post("/{ledger_id}/members", response_model=MemberResponse, status_code=status.HTTP_201_CREATED)
def add_member(
    ledger_id: UUID,
    request: AddMemberRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add a member to the ledger (only owner can add)"""
    ledger = get_ledger_or_404(db, ledger_id)
    logger.info(
        "添加账本成员 ledger_id=%s 操作者=%s user_id=%s 临时成员=%s 临时名=%s",
        ledger_id,
        current_user.id,
        request.user_id,
        request.is_temporary,
        request.temporary_name,
    )

    if ledger.owner_id != current_user.id:
        logger.warning(
            "非所有者尝试添加成员 ledger_id=%s 操作者=%s 所有者=%s",
            ledger_id,
            current_user.id,
            ledger.owner_id,
        )
        raise HTTPException(status_code=403, detail="Only owner can add members")

    # Handle temporary members
    if request.is_temporary:
        if not request.temporary_name:
            logger.warning("添加临时成员缺少名称 ledger_id=%s 操作者=%s", ledger_id, current_user.id)
            raise HTTPException(status_code=400, detail="Temporary name is required")
        
        # Check if temporary member already exists
        existing = db.query(LedgerMember).filter(
            LedgerMember.ledger_id == ledger_id,
            LedgerMember.user_id.is_(None),
            LedgerMember.temporary_name == request.temporary_name,
        ).first()

        if existing and existing.status == "removed":
            existing.status = "active"
            existing.nickname = request.temporary_name
            db.commit()
            db.refresh(existing)
            return MemberResponse(
                id=existing.id,
                user_id=existing.user_id,
                nickname=existing.nickname,
                joined_at=existing.joined_at,
                user=None,
                is_temporary=True,
                temporary_name=existing.temporary_name,
                status=existing.status,
            )

        if existing:
            logger.info(
                "拒绝重复添加临时成员 ledger_id=%s 临时名=%s",
                ledger_id,
                request.temporary_name,
            )
            raise HTTPException(status_code=400, detail="Temporary member already exists")

        member = LedgerMember(
            ledger_id=ledger_id,
            user_id=None,
            nickname=request.temporary_name,
            is_temporary=True,
            temporary_name=request.temporary_name,
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        logger.info("已添加临时成员 ledger_id=%s member_id=%s", ledger_id, member.id)

        return MemberResponse(
            id=member.id,
            user_id=member.user_id,
            nickname=member.nickname,
            joined_at=member.joined_at,
            user=None,
            is_temporary=True,
            temporary_name=member.temporary_name
        )

    # Handle regular members
    if not request.user_id:
        logger.warning("添加正式成员缺少 user_id ledger_id=%s 操作者=%s", ledger_id, current_user.id)
        raise HTTPException(status_code=400, detail="User ID is required for non-temporary members")

    # Check if user exists
    user = db.query(User).filter(User.id == request.user_id).first()
    if not user:
        logger.warning("添加正式成员：用户不存在 ledger_id=%s user_id=%s", ledger_id, request.user_id)
        raise HTTPException(status_code=404, detail="User not found")

    # Check if already a member (regular or temporary)
    existing = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id
    ).filter(
        LedgerMember.user_id == request.user_id
    ).first()

    # Re-invite users who previously left or declined.
    if existing and existing.status in ("removed", "rejected"):
        previous_status = existing.status
        existing.status = "pending"
        db.commit()
        db.refresh(existing)
        logger.info(
            "重新邀请正式成员 ledger_id=%s member_id=%s user_id=%s 原状态=%s",
            ledger_id,
            existing.id,
            request.user_id,
            previous_status,
        )
        invitation_cache.invalidate_pending_invitations(request.user_id)
        send_push_safely(db, [request.user_id], build_payload(
            event=PushEvent.LEDGER_INVITED,
            actor_name=current_user.display_name or current_user.username,
            ledger_name=ledger.name,
            ledger_id=str(ledger_id),
        ))
        return MemberResponse(
            id=existing.id,
            user_id=existing.user_id,
            nickname=existing.nickname,
            joined_at=existing.joined_at,
            user=user_to_response(user),
            is_temporary=False,
            temporary_name=None,
            status=existing.status,
        )

    if existing:
        logger.info(
            "拒绝重复添加正式成员 ledger_id=%s user_id=%s 状态=%s",
            ledger_id,
            request.user_id,
            existing.status,
        )
        detail = (
            "Invitation already pending"
            if existing.status == "pending"
            else "User is already a member"
        )
        raise HTTPException(status_code=400, detail=detail)

    # Add member
    member = LedgerMember(
        ledger_id=ledger_id,
        user_id=request.user_id,
        status="pending",
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    logger.info("已邀请正式成员 ledger_id=%s member_id=%s user_id=%s", ledger_id, member.id, request.user_id)

    invitation_cache.invalidate_pending_invitations(request.user_id)
    send_push_safely(db, [request.user_id], build_payload(
        event=PushEvent.LEDGER_INVITED,
        actor_name=current_user.display_name or current_user.username,
        ledger_name=ledger.name,
        ledger_id=str(ledger_id),
    ))

    return MemberResponse(
        id=member.id,
        user_id=member.user_id,
        nickname=member.nickname,
        joined_at=member.joined_at,
        user=user_to_response(user),
        is_temporary=False,
        temporary_name=None,
        status="pending",
    )


@router.get("/{ledger_id}/members", response_model=List[MemberResponse])
def get_members(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all members of a ledger (including pending/rejected invitations)."""
    require_ledger_member(db, ledger_id, current_user)

    members = (
        db.query(LedgerMember)
        .options(joinedload(LedgerMember.user))
        .filter(LedgerMember.ledger_id == ledger_id)
        .all()
    )
    result = []
    for m in members:
        user = m.user if m.user_id and not m.is_temporary else None
        result.append(MemberResponse(
            id=m.id,
            user_id=m.user_id,
            nickname=m.nickname,
            joined_at=m.joined_at,
            user=user_to_response(user) if user else None,
            is_temporary=m.is_temporary,
            temporary_name=m.temporary_name,
            status=m.status,
        ))
    return result


def member_has_history(db: Session, ledger_id: UUID, membership: LedgerMember) -> bool:
    expense_reference = db.query(ExpenseSplit.id).join(Expense).filter(
        Expense.ledger_id == ledger_id,
        ExpenseSplit.member_id == membership.id,
    ).first()
    if expense_reference:
        return True

    if membership.user_id is None:
        return False

    return bool(
        db.query(Expense.id).filter(
            Expense.ledger_id == ledger_id,
            or_(
                Expense.payer_id == membership.user_id,
                Expense.created_by == membership.user_id,
            ),
        ).first()
        or db.query(Settlement.id).filter(
            Settlement.ledger_id == ledger_id,
            or_(
                Settlement.from_user_id == membership.user_id,
                Settlement.to_user_id == membership.user_id,
            ),
        ).first()
    )


def member_balance(db: Session, ledger_id: UUID, membership: LedgerMember) -> Decimal:
    """Return signed paid - owed + settled for a member across non-rejected entries."""
    from app.services.settlement import expense_net_amount, expense_scaled_split_amounts

    net = Decimal("0")
    rows = (
        db.query(Expense)
        .options(joinedload(Expense.splits))
        .filter(
            Expense.ledger_id == ledger_id,
            Expense.status != ExpenseStatus.REJECTED,
        )
        .all()
    )
    for expense in rows:
        if membership.user_id is not None and expense.payer_id == membership.user_id:
            net += expense_net_amount(expense)
        for split, amount in expense_scaled_split_amounts(expense):
            if split.member_id == membership.id or (
                membership.user_id is not None and split.user_id == membership.user_id
            ):
                net -= amount

    if membership.user_id is None:
        return net

    settled_out = (
        db.query(func.coalesce(func.sum(Settlement.amount), 0))
        .filter(
            Settlement.ledger_id == ledger_id,
            Settlement.from_user_id == membership.user_id,
        )
        .scalar()
    )
    settled_in = (
        db.query(func.coalesce(func.sum(Settlement.amount), 0))
        .filter(
            Settlement.ledger_id == ledger_id,
            Settlement.to_user_id == membership.user_id,
        )
        .scalar()
    )
    return (
        net
        + Decimal(str(settled_out or 0))
        - Decimal(str(settled_in or 0))
    )


def remove_membership(db: Session, ledger_id: UUID, membership: LedgerMember):
    """Remove a settled member while retaining records needed by history."""
    balance = member_balance(db, ledger_id, membership)
    if abs(balance) >= Decimal("0.01"):
        raise HTTPException(
            status_code=400,
            detail=f"Member still has an unsettled balance ({balance})",
        )

    if member_has_history(db, ledger_id, membership):
        membership.status = "removed"
    else:
        db.delete(membership)


@router.delete("/{ledger_id}/members/me", status_code=status.HTTP_204_NO_CONTENT)
def leave_ledger(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Current user leaves the ledger (non-owner only)"""
    ledger = get_ledger_or_404(db, ledger_id)

    if ledger.owner_id == current_user.id:
        raise HTTPException(status_code=400, detail="Owner cannot leave, must delete ledger")

    membership = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.user_id == current_user.id
    ).first()

    if not membership:
        raise HTTPException(status_code=404, detail="Not a member of this ledger")

    remove_membership(db, ledger_id, membership)
    db.commit()
    return None


@router.delete("/{ledger_id}/members/{member_identifier}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    ledger_id: UUID,
    member_identifier: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Remove a member by user ID or ledger member record ID."""
    ledger = get_ledger_or_404(db, ledger_id)

    membership = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        (LedgerMember.user_id == member_identifier) | (LedgerMember.id == member_identifier),
    ).first()

    if not membership:
        raise HTTPException(status_code=404, detail="Member not found")

    # Removing members is an owner-only operation. Members who want to leave
    # must use the dedicated /members/me endpoint.
    if current_user.id != ledger.owner_id:
        raise HTTPException(status_code=403, detail="Only owner can remove members")

    # Owner cannot remove themselves
    if membership.user_id == ledger.owner_id:
        raise HTTPException(status_code=400, detail="Owner cannot be removed")

    remove_membership(db, ledger_id, membership)
    db.commit()
    return None
