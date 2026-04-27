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
    MemberCreate,
)
from app.schemas.user import UserResponse
from app.utils.deps import get_current_user, get_ledger_or_404, require_ledger_member

router = APIRouter(prefix="/ledgers", tags=["ledgers"])


@router.post("", response_model=LedgerWithMembers, status_code=status.HTTP_201_CREATED)
def create_ledger(
    ledger: LedgerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new ledger and add the creator as owner member"""
    
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
        nickname=current_user.display_name,
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
                nickname=member_data.nickname or user.display_name,
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
            temporary_name=member.temporary_name
        ))

    db.commit()

    # Build response with all members including owner
    owner_user_response = UserResponse.model_validate(current_user)
    owner_member_response = MemberResponse(
        id=owner_member.id,
        user_id=current_user.id,
        nickname=current_user.display_name,
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
    ledger = get_ledger_or_404(db, ledger_id)
    require_ledger_member(db, ledger_id, current_user)

    # Get members with user details
    members = db.query(LedgerMember).filter(LedgerMember.ledger_id == ledger_id).all()
    member_responses = []
    for m in members:
        user = db.query(User).filter(User.id == m.user_id).first() if m.user_id and not m.is_temporary else None
        member_responses.append(MemberResponse(
            id=m.id,
            user_id=m.user_id,
            nickname=m.nickname,
            joined_at=m.joined_at,
            user=UserResponse.model_validate(user) if user else None,
            is_temporary=m.is_temporary,
            temporary_name=m.temporary_name
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

    if ledger.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only owner can add members")

    # Handle temporary members
    if request.is_temporary:
        if not request.temporary_name:
            raise HTTPException(status_code=400, detail="Temporary name is required")
        
        # Check if temporary member already exists
        existing = db.query(LedgerMember).filter(
            LedgerMember.ledger_id == ledger_id,
            LedgerMember.is_temporary == True,
            LedgerMember.temporary_name == request.temporary_name
        ).first()

        if existing:
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
        raise HTTPException(status_code=400, detail="User ID is required for non-temporary members")

    # Check if user exists
    user = db.query(User).filter(User.id == request.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if already a member (regular or temporary)
    existing = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id
    ).filter(
        (LedgerMember.user_id == request.user_id) | 
        ((LedgerMember.is_temporary == True) & (LedgerMember.temporary_name == request.nickname))
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
        id=member.id,
        user_id=member.user_id,
        nickname=member.nickname,
        joined_at=member.joined_at,
        user=UserResponse.model_validate(user),
        is_temporary=False,
        temporary_name=None
    )


@router.get("/{ledger_id}/members", response_model=List[MemberResponse])
def get_members(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all members of a ledger"""
    require_ledger_member(db, ledger_id, current_user)

    members = db.query(LedgerMember).filter(LedgerMember.ledger_id == ledger_id).all()
    result = []
    for m in members:
        user = db.query(User).filter(User.id == m.user_id).first() if m.user_id and not m.is_temporary else None
        result.append(MemberResponse(
            id=m.id,
            user_id=m.user_id,
            nickname=m.nickname,
            joined_at=m.joined_at,
            user=UserResponse.model_validate(user) if user else None,
            is_temporary=m.is_temporary,
            temporary_name=m.temporary_name
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
    ledger = get_ledger_or_404(db, ledger_id)

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
    ledger = get_ledger_or_404(db, ledger_id)

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
