from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models import User, Ledger, LedgerMember, Settlement
from app.schemas.settlement import (
    SettlementCreate,
    SettlementResponse,
    SettlementWithUsers,
    SettlementInstruction,
)
from app.schemas.user import UserResponse
from app.services.settlement import SettlementCalculator, create_settlement_record
from app.utils.deps import get_current_user

router = APIRouter(prefix="/ledgers", tags=["settlements"])


@router.get("/{ledger_id}/settlements", response_model=List[SettlementInstruction])
def get_settlements(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Calculate and return settlement instructions for a ledger"""
    # Check if ledger exists and user is a member
    ledger = db.query(Ledger).filter(Ledger.id == ledger_id).first()
    if not ledger:
        raise HTTPException(status_code=404, detail="Ledger not found")

    membership = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.user_id == current_user.id
    ).first()

    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this ledger")

    # Calculate settlements
    calculator = SettlementCalculator(db, ledger_id)
    settlements = calculator.calculate_settlements()

    return [
        SettlementInstruction(
            from_user_id=s["from_user_id"],
            from_user_name=s["from_user_name"],
            to_user_id=s["to_user_id"],
            to_user_name=s["to_user_name"],
            amount=s["amount"],
        )
        for s in settlements
    ]


@router.get("/{ledger_id}/settlements/history", response_model=List[SettlementWithUsers])
def get_settlement_history(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get settlement history for a ledger"""
    # Check if ledger exists and user is a member
    ledger = db.query(Ledger).filter(Ledger.id == ledger_id).first()
    if not ledger:
        raise HTTPException(status_code=404, detail="Ledger not found")

    membership = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.user_id == current_user.id
    ).first()

    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this ledger")

    settlements = db.query(Settlement).filter(Settlement.ledger_id == ledger_id).order_by(Settlement.settled_at.desc()).all()

    result = []
    for s in settlements:
        from_user = db.query(User).filter(User.id == s.from_user_id).first()
        to_user = db.query(User).filter(User.id == s.to_user_id).first()

        result.append(SettlementWithUsers(
            id=s.id,
            ledger_id=s.ledger_id,
            from_user_id=s.from_user_id,
            to_user_id=s.to_user_id,
            amount=s.amount,
            note=s.note,
            settled_at=s.settled_at,
            from_user=UserResponse.model_validate(from_user),
            to_user=UserResponse.model_validate(to_user),
        ))

    return result


@router.post("/{ledger_id}/settlements", response_model=SettlementResponse, status_code=status.HTTP_201_CREATED)
def create_settlement(
    ledger_id: UUID,
    settlement: SettlementCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Record a settlement (payment)"""
    # Check if ledger exists and user is a member
    ledger = db.query(Ledger).filter(Ledger.id == ledger_id).first()
    if not ledger:
        raise HTTPException(status_code=404, detail="Ledger not found")

    membership = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.user_id == current_user.id
    ).first()

    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this ledger")

    # Validate from_user and to_user are members
    from_member = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.user_id == settlement.from_user_id
    ).first()

    to_member = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.user_id == settlement.to_user_id
    ).first()

    if not from_member or not to_member:
        raise HTTPException(status_code=400, detail="Both users must be ledger members")

    # Create settlement record
    db_settlement = create_settlement_record(
        db=db,
        ledger_id=ledger_id,
        from_user_id=settlement.from_user_id,
        to_user_id=settlement.to_user_id,
        amount=settlement.amount,
        note=settlement.note,
    )

    return db_settlement
