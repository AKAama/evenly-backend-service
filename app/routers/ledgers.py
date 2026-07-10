import logging
from decimal import Decimal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import joinedload, Session
from typing import List

from app.database import get_db
from app.models import (
    User,
    Ledger,
    LedgerMember,
    Expense,
    ExpenseConfirmation,
    ExpenseSplit,
    ExpenseStatus,
    Settlement,
)
from app.schemas.ledger import (
    LedgerCreate,
    LedgerResponse,
    LedgerWithMembers,
    AddMemberRequest,
    MemberResponse,
    MemberCreate,
    LedgerInvitationResponse,
    LedgerMemberWithUser,
    LedgerOverviewResponse,
)
from app.schemas.expense import (
    ExpenseConfirmationResponse,
    ExpenseSplitResponse,
    ExpenseWithDetails,
)
from app.schemas.settlement import SettlementInstruction
from app.schemas.user import UserResponse
from app.utils.deps import get_current_user, get_ledger_or_404, require_ledger_member

router = APIRouter(prefix="/ledgers", tags=["ledgers"])
logger = logging.getLogger(__name__)


@router.post("", response_model=LedgerWithMembers, status_code=status.HTTP_201_CREATED)
def create_ledger(
    ledger: LedgerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new ledger and add the creator as owner member"""
    logger.info(
        "Creating ledger name=%s owner_id=%s initial_members=%d",
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
            user=UserResponse.model_validate(member_user) if member_user else None,
            is_temporary=member.is_temporary,
                temporary_name=member.temporary_name,
            status=member.status,
        ))

    db.commit()

    # Build response with all members including owner
    owner_user_response = UserResponse.model_validate(current_user)
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
        created_at=db_ledger.created_at,
        updated_at=db_ledger.updated_at,
        members=all_members
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
        .filter(Ledger.id.in_(member_ledger_ids))
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
    return [
        LedgerInvitationResponse(
            id=membership.id,
            ledger_id=ledger.id,
            ledger_name=ledger.name,
            invited_by_name=inviter.display_name,
            created_at=membership.created_at,
        )
        for membership, ledger, inviter in rows
    ]


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


@router.post("/invitations/{invitation_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
def reject_invitation(
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
    db.delete(invitation)
    db.commit()


@router.get("/{ledger_id}", response_model=LedgerWithMembers)
def get_ledger(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get ledger details with members"""
    ledger = get_ledger_or_404(db, ledger_id)
    require_ledger_member(db, ledger_id, current_user)

    # Get members with user details (include pending so owner can see outstanding invitations)
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
            user=UserResponse.model_validate(user) if user else None,
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
            user=UserResponse.model_validate(member.user) if member.user else None,
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
            required_ids = {
                split.user_id for split in expense.splits if split.user_id is not None
            } - {expense.created_by}
            confirmed_ids = {
                confirmation.user_id
                for confirmation in expense.confirmations
                if confirmation.status == "confirmed"
            }
            if required_ids <= confirmed_ids:
                effective_status = ExpenseStatus.CONFIRMED

        expense_responses.append(ExpenseWithDetails(
            id=expense.id,
            ledger_id=expense.ledger_id,
            payer_id=expense.payer_id,
            created_by=expense.created_by,
            title=expense.title,
            total_amount=expense.total_amount,
            note=expense.note,
            expense_date=expense.expense_date,
            status=effective_status.value,
            created_at=expense.created_at,
            updated_at=expense.updated_at,
            payer=UserResponse.model_validate(expense.payer),
            splits=[ExpenseSplitResponse.model_validate(split) for split in expense.splits],
            confirmations=[
                ExpenseConfirmationResponse.model_validate(confirmation)
                for confirmation in expense.confirmations
            ],
        ))

    history = []

    registered_members = [member for member in active_members if member.user_id is not None]
    balances = {member.user_id: Decimal("0") for member in registered_members}
    names = {
        member.user_id: (
            member.user.display_name or member.user.email
            if member.user else member.nickname or "Unknown"
        )
        for member in registered_members
    }
    for expense in expenses:
        if expense.status != ExpenseStatus.CONFIRMED:
            continue
        if expense.payer_id in balances:
            balances[expense.payer_id] += expense.total_amount
        for split in expense.splits:
            if split.user_id in balances:
                balances[split.user_id] -= split.amount
    creditors = sorted(
        [(user_id, amount) for user_id, amount in balances.items() if amount > 0],
        key=lambda item: item[1],
        reverse=True,
    )
    debtors = sorted(
        [(user_id, -amount) for user_id, amount in balances.items() if amount < 0],
        key=lambda item: item[1],
        reverse=True,
    )
    suggestions = []
    creditor_index = debtor_index = 0
    while creditor_index < len(creditors) and debtor_index < len(debtors):
        creditor_id, credit = creditors[creditor_index]
        debtor_id, debt = debtors[debtor_index]
        amount = min(credit, debt)
        suggestions.append(SettlementInstruction(
            from_user_id=debtor_id,
            from_user_name=names.get(debtor_id, "Unknown"),
            to_user_id=creditor_id,
            to_user_name=names.get(creditor_id, "Unknown"),
            amount=amount,
        ))
        creditors[creditor_index] = (creditor_id, credit - amount)
        debtors[debtor_index] = (debtor_id, debt - amount)
        if creditors[creditor_index][1] <= 0:
            creditor_index += 1
        if debtors[debtor_index][1] <= 0:
            debtor_index += 1

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
        "Adding ledger member ledger_id=%s actor_id=%s user_id=%s is_temporary=%s temporary_name=%s",
        ledger_id,
        current_user.id,
        request.user_id,
        request.is_temporary,
        request.temporary_name,
    )

    if ledger.owner_id != current_user.id:
        logger.warning(
            "Non-owner add member attempt ledger_id=%s actor_id=%s owner_id=%s",
            ledger_id,
            current_user.id,
            ledger.owner_id,
        )
        raise HTTPException(status_code=403, detail="Only owner can add members")

    # Handle temporary members
    if request.is_temporary:
        if not request.temporary_name:
            logger.warning("Temporary member add missing name ledger_id=%s actor_id=%s", ledger_id, current_user.id)
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
                "Duplicate temporary member add rejected ledger_id=%s temporary_name=%s",
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
        logger.info("Added temporary member ledger_id=%s member_id=%s", ledger_id, member.id)

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
        logger.warning("Regular member add missing user_id ledger_id=%s actor_id=%s", ledger_id, current_user.id)
        raise HTTPException(status_code=400, detail="User ID is required for non-temporary members")

    # Check if user exists
    user = db.query(User).filter(User.id == request.user_id).first()
    if not user:
        logger.warning("Regular member add user not found ledger_id=%s user_id=%s", ledger_id, request.user_id)
        raise HTTPException(status_code=404, detail="User not found")

    # Check if already a member (regular or temporary)
    existing = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id
    ).filter(
        LedgerMember.user_id == request.user_id
    ).first()

    if existing and existing.status == "removed":
        existing.status = "pending"
        db.commit()
        db.refresh(existing)
        return MemberResponse(
            id=existing.id,
            user_id=existing.user_id,
            nickname=existing.nickname,
            joined_at=existing.joined_at,
            user=UserResponse.model_validate(user),
            is_temporary=False,
            temporary_name=None,
            status=existing.status,
        )

    if existing:
        logger.info("Duplicate regular member add rejected ledger_id=%s user_id=%s", ledger_id, request.user_id)
        raise HTTPException(status_code=400, detail="User is already a member")

    # Add member
    member = LedgerMember(
        ledger_id=ledger_id,
        user_id=request.user_id,
        status="pending",
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    logger.info("Invited regular member ledger_id=%s member_id=%s user_id=%s", ledger_id, member.id, request.user_id)

    return MemberResponse(
        id=member.id,
        user_id=member.user_id,
        nickname=member.nickname,
        joined_at=member.joined_at,
        user=UserResponse.model_validate(user),
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
    """Get all members of a ledger (including pending invitations so members can see who hasn't joined yet)"""
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
            user=UserResponse.model_validate(user) if user else None,
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
    """Return paid - owed + settled for a member across non-rejected expenses."""
    owed = (
        db.query(func.coalesce(func.sum(ExpenseSplit.amount), 0))
        .join(Expense)
        .filter(
            Expense.ledger_id == ledger_id,
            Expense.status != ExpenseStatus.REJECTED,
            ExpenseSplit.member_id == membership.id,
        )
        .scalar()
    )

    if membership.user_id is None:
        return -Decimal(str(owed or 0))

    paid = (
        db.query(func.coalesce(func.sum(Expense.total_amount), 0))
        .filter(
            Expense.ledger_id == ledger_id,
            Expense.status != ExpenseStatus.REJECTED,
            Expense.payer_id == membership.user_id,
        )
        .scalar()
    )
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
        Decimal(str(paid or 0))
        - Decimal(str(owed or 0))
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
