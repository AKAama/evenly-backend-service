from uuid import UUID
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models import User, Ledger, LedgerMember, Expense, ExpenseSplit, ExpenseConfirmation, ExpenseStatus
from app.schemas.expense import (
    ExpenseCreate,
    ExpenseResponse,
    ExpenseWithDetails,
    ConfirmExpenseRequest,
    ExpenseSplitResponse,
    ExpenseConfirmationResponse,
)
from app.schemas.user import UserResponse
from app.utils.deps import get_current_user, get_ledger_or_404, require_ledger_member

router = APIRouter(prefix="/expenses", tags=["expenses"])

CENT = Decimal("0.01")


def normalize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT)


@router.post("/ledgers/{ledger_id}/expenses", response_model=ExpenseResponse, status_code=status.HTTP_201_CREATED)
def create_expense(
    ledger_id: UUID,
    expense: ExpenseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new expense in a ledger"""
    # Check if ledger exists and user is a member
    get_ledger_or_404(db, ledger_id)
    require_ledger_member(db, ledger_id, current_user)

    if expense.total_amount <= 0:
        raise HTTPException(status_code=400, detail="Expense amount must be greater than zero")

    if not expense.splits:
        raise HTTPException(status_code=400, detail="At least one split is required")

    member_ids = {
        m.user_id
        for m in db.query(LedgerMember).filter(LedgerMember.ledger_id == ledger_id).all()
        if m.user_id is not None and not m.is_temporary
    }

    split_user_ids = [s.user_id for s in expense.splits]
    if len(split_user_ids) != len(set(split_user_ids)):
        raise HTTPException(status_code=400, detail="Duplicate users in splits are not allowed")

    if expense.payer_id not in member_ids:
        raise HTTPException(status_code=400, detail="Payer must be a registered ledger member")

    invalid_split_ids = set(split_user_ids) - member_ids
    if invalid_split_ids:
        raise HTTPException(status_code=400, detail="All split users must be registered ledger members")

    # Validate splits total equals expense total
    split_total = normalize_money(sum(s.amount for s in expense.splits))
    expense_total = normalize_money(expense.total_amount)
    if split_total != expense_total:
        raise HTTPException(
            status_code=400,
            detail=f"Split total ({split_total}) must equal expense amount ({expense_total})"
        )

    # Validate payer is in splits
    payer_in_splits = any(s.user_id == expense.payer_id for s in expense.splits)
    if not payer_in_splits:
        raise HTTPException(status_code=400, detail="Payer must be included in splits")

    # Create expense
    db_expense = Expense(
        ledger_id=ledger_id,
        payer_id=expense.payer_id,
        created_by=current_user.id,
        title=expense.title,
        total_amount=expense.total_amount,
        note=expense.note,
        expense_date=expense.expense_date,
        status=ExpenseStatus.PENDING,
    )
    db.add(db_expense)
    db.commit()
    db.refresh(db_expense)

    # Create splits
    for split in expense.splits:
        db_split = ExpenseSplit(
            expense_id=db_expense.id,
            user_id=split.user_id,
            amount=split.amount,
        )
        db.add(db_split)

    db.commit()
    db.refresh(db_expense)

    return db_expense


@router.get("/ledgers/{ledger_id}/expenses", response_model=List[ExpenseWithDetails])
def get_expenses(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all expenses in a ledger"""
    # Check if user is a member
    require_ledger_member(db, ledger_id, current_user)

    expenses = db.query(Expense).filter(Expense.ledger_id == ledger_id).order_by(Expense.created_at.desc()).all()

    result = []
    for exp in expenses:
        payer = db.query(User).filter(User.id == exp.payer_id).first()
        splits = db.query(ExpenseSplit).filter(ExpenseSplit.expense_id == exp.id).all()
        confirmations = db.query(ExpenseConfirmation).filter(ExpenseConfirmation.expense_id == exp.id).all()

        response = ExpenseWithDetails(
            id=exp.id,
            ledger_id=exp.ledger_id,
            payer_id=exp.payer_id,
            created_by=exp.created_by,
            title=exp.title,
            total_amount=exp.total_amount,
            note=exp.note,
            expense_date=exp.expense_date,
            status=exp.status.value,
            created_at=exp.created_at,
            updated_at=exp.updated_at,
            payer=UserResponse.model_validate(payer),
            splits=[
                ExpenseSplitResponse(
                    id=s.id,
                    expense_id=s.expense_id,
                    user_id=s.user_id,
                    amount=s.amount,
                    created_at=s.created_at
                )
                for s in splits
            ],
            confirmations=[
                ExpenseConfirmationResponse(
                    id=c.id,
                    expense_id=c.expense_id,
                    user_id=c.user_id,
                    status=c.status,
                    created_at=c.created_at
                )
                for c in confirmations
            ]
        )
        result.append(response)

    return result


@router.post("/{expense_id}/confirm", response_model=ExpenseResponse)
def confirm_expense(
    expense_id: UUID,
    request: ConfirmExpenseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Confirm or reject an expense"""
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    # Check if user is a member of the ledger
    require_ledger_member(db, expense.ledger_id, current_user)

    split_participants = {
        s.user_id
        for s in db.query(ExpenseSplit).filter(ExpenseSplit.expense_id == expense_id).all()
    }
    if current_user.id not in split_participants:
        raise HTTPException(status_code=403, detail="Only expense participants can confirm this expense")

    # Check if already confirmed or rejected
    if expense.status != ExpenseStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Expense is already {expense.status.value}")

    # Validate status
    if request.status not in ["confirmed", "rejected"]:
        raise HTTPException(status_code=400, detail="Status must be 'confirmed' or 'rejected'")

    # Check if user already confirmed/rejected this expense
    existing = db.query(ExpenseConfirmation).filter(
        ExpenseConfirmation.expense_id == expense_id,
        ExpenseConfirmation.user_id == current_user.id
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="You have already responded to this expense")

    # Create confirmation record
    confirmation = ExpenseConfirmation(
        expense_id=expense_id,
        user_id=current_user.id,
        status=request.status,
    )
    db.add(confirmation)
    db.flush()  # Flush to get the new confirmation in the query

    # Check if all members have confirmed
    if request.status == "confirmed":
        # Check if all members have now confirmed
        confirmations = db.query(ExpenseConfirmation).filter(
            ExpenseConfirmation.expense_id == expense_id,
            ExpenseConfirmation.status == "confirmed"
        ).all()
        confirmed_ids = {c.user_id for c in confirmations}

        if split_participants <= confirmed_ids:
            expense.status = ExpenseStatus.CONFIRMED

    elif request.status == "rejected":
        expense.status = ExpenseStatus.REJECTED

    db.commit()
    db.refresh(expense)

    return expense


@router.post("/{expense_id}/reject", response_model=ExpenseResponse)
def reject_expense(
    expense_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Reject an expense (alias for confirm with rejected status)"""
    return confirm_expense(expense_id, ConfirmExpenseRequest(status="rejected"), db, current_user)


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_expense(
    expense_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete an expense (only creator, and only if pending)"""
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    if expense.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only creator can delete this expense")

    if expense.status != ExpenseStatus.PENDING:
        raise HTTPException(status_code=400, detail="Can only delete pending expenses")

    db.delete(expense)
    db.commit()
    return None


@router.get("/{expense_id}", response_model=ExpenseWithDetails)
def get_expense(
    expense_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get expense details"""
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    # Check if user is a member
    require_ledger_member(db, expense.ledger_id, current_user)

    payer = db.query(User).filter(User.id == expense.payer_id).first()
    splits = db.query(ExpenseSplit).filter(ExpenseSplit.expense_id == expense.id).all()
    confirmations = db.query(ExpenseConfirmation).filter(ExpenseConfirmation.expense_id == expense.id).all()

    return ExpenseWithDetails(
        id=expense.id,
        ledger_id=expense.ledger_id,
        payer_id=expense.payer_id,
        created_by=expense.created_by,
        title=expense.title,
        total_amount=expense.total_amount,
        note=expense.note,
        expense_date=expense.expense_date,
        status=expense.status.value,
        created_at=expense.created_at,
        updated_at=expense.updated_at,
        payer=UserResponse.model_validate(payer),
        splits=[
            ExpenseSplitResponse(
                id=s.id,
                expense_id=s.expense_id,
                user_id=s.user_id,
                amount=s.amount,
                created_at=s.created_at
            )
            for s in splits
        ],
        confirmations=[
            ExpenseConfirmationResponse(
                id=c.id,
                expense_id=c.expense_id,
                user_id=c.user_id,
                status=c.status,
                created_at=c.created_at
            )
            for c in confirmations
        ]
    )
