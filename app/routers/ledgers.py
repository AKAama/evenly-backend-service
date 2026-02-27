from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models import User, Ledger, LedgerMember
from app.schemas.ledger import (
    LedgerCreate,
    LedgerResponse,
    LedgerWithMembers,
    AddMemberRequest,
    MemberResponse,
)
from app.schemas.user import UserResponse
from app.utils.deps import get_current_user

router = APIRouter(prefix="/ledgers", tags=["ledgers"])


@router.post("", response_model=LedgerResponse, status_code=status.HTTP_201_CREATED)
def create_ledger(
    ledger: LedgerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new ledger and add the creator as owner member"""
    db_ledger = Ledger(
        name=ledger.name,
        owner_id=current_user.id,
        currency=ledger.currency,
    )
    db.add(db_ledger)
    db.commit()
    db.refresh(db_ledger)

    # Add creator as a member
    member = LedgerMember(
        ledger_id=db_ledger.id,
        user_id=current_user.id,
        nickname=current_user.display_name,
    )
    db.add(member)
    db.commit()

    return db_ledger


@router.get("", response_model=List[LedgerResponse])
def get_ledgers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all ledgers the current user is a member of"""
    member_query = db.query(LedgerMember).filter(LedgerMember.user_id == current_user.id)
    member_ledger_ids = [m.ledger_id for m in member_query.all()]

    ledgers = db.query(Ledger).filter(Ledger.id.in_(member_ledger_ids)).all()
    return ledgers


@router.get("/{ledger_id}", response_model=LedgerWithMembers)
def get_ledger(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get ledger details with members"""
    ledger = db.query(Ledger).filter(Ledger.id == ledger_id).first()
    if not ledger:
        raise HTTPException(status_code=404, detail="Ledger not found")

    # Check if user is a member
    membership = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.user_id == current_user.id
    ).first()

    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this ledger")

    # Get members with user details
    members = db.query(LedgerMember).filter(LedgerMember.ledger_id == ledger_id).all()
    member_responses = []
    for m in members:
        user = db.query(User).filter(User.id == m.user_id).first()
        member_responses.append(MemberResponse(
            user_id=m.user_id,
            nickname=m.nickname,
            joined_at=m.joined_at,
            user=UserResponse.model_validate(user)
        ))

    response = LedgerWithMembers.model_validate(ledger)
    response.members = member_responses
    return response


@router.delete("/{ledger_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ledger(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a ledger (only owner can delete)"""
    ledger = db.query(Ledger).filter(Ledger.id == ledger_id).first()
    if not ledger:
        raise HTTPException(status_code=404, detail="Ledger not found")

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
    ledger = db.query(Ledger).filter(Ledger.id == ledger_id).first()
    if not ledger:
        raise HTTPException(status_code=404, detail="Ledger not found")

    if ledger.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only owner can add members")

    # Check if user exists
    user = db.query(User).filter(User.id == request.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if already a member
    existing = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.user_id == request.user_id
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="User is already a member")

    # Add member
    member = LedgerMember(
        ledger_id=ledger_id,
        user_id=request.user_id,
        nickname=request.nickname or user.display_name,
    )
    db.add(member)
    db.commit()
    db.refresh(member)

    return MemberResponse(
        user_id=member.user_id,
        nickname=member.nickname,
        joined_at=member.joined_at,
        user=UserResponse.model_validate(user)
    )


@router.get("/{ledger_id}/members", response_model=List[MemberResponse])
def get_members(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all members of a ledger"""
    # Check if user is a member
    membership = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.user_id == current_user.id
    ).first()

    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this ledger")

    members = db.query(LedgerMember).filter(LedgerMember.ledger_id == ledger_id).all()
    result = []
    for m in members:
        user = db.query(User).filter(User.id == m.user_id).first()
        result.append(MemberResponse(
            user_id=m.user_id,
            nickname=m.nickname,
            joined_at=m.joined_at,
            user=UserResponse.model_validate(user)
        ))
    return result


@router.delete("/{ledger_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    ledger_id: UUID,
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Remove a member from ledger (owner can remove others, members can remove themselves)"""
    ledger = db.query(Ledger).filter(Ledger.id == ledger_id).first()
    if not ledger:
        raise HTTPException(status_code=404, detail="Ledger not found")

    membership = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.user_id == user_id
    ).first()

    if not membership:
        raise HTTPException(status_code=404, detail="Member not found")

    # Check permission: owner can remove anyone, member can only remove themselves
    if current_user.id != ledger.owner_id and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to remove this member")

    # Owner cannot remove themselves
    if ledger.owner_id == user_id:
        raise HTTPException(status_code=400, detail="Owner cannot be removed")

    db.delete(membership)
    db.commit()
    return None


@router.delete("/{ledger_id}/members/me", status_code=status.HTTP_204_NO_CONTENT)
def leave_ledger(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Current user leaves the ledger (non-owner only)"""
    ledger = db.query(Ledger).filter(Ledger.id == ledger_id).first()
    if not ledger:
        raise HTTPException(status_code=404, detail="Ledger not found")

    if ledger.owner_id == current_user.id:
        raise HTTPException(status_code=400, detail="Owner cannot leave, must delete ledger")

    membership = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.user_id == current_user.id
    ).first()

    if not membership:
        raise HTTPException(status_code=404, detail="Not a member of this ledger")

    db.delete(membership)
    db.commit()
    return None
