from datetime import datetime, date
from uuid import UUID
from decimal import Decimal
from pydantic import BaseModel, ConfigDict

from app.schemas.user import UserResponse


class ExpenseSplitBase(BaseModel):
    user_id: UUID
    amount: Decimal


class ExpenseSplitCreate(ExpenseSplitBase):
    pass


class ExpenseSplitResponse(ExpenseSplitBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    expense_id: UUID
    created_at: datetime


class ExpenseConfirmationBase(BaseModel):
    status: str  # 'confirmed' or 'rejected'


class ExpenseConfirmationResponse(ExpenseConfirmationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    expense_id: UUID
    user_id: UUID
    created_at: datetime


class ExpenseBase(BaseModel):
    title: str | None = None
    total_amount: Decimal
    note: str | None = None
    expense_date: date


class ExpenseCreate(ExpenseBase):
    payer_id: UUID
    splits: list[ExpenseSplitCreate]


class ExpenseResponse(ExpenseBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ledger_id: UUID
    payer_id: UUID
    created_by: UUID
    status: str
    created_at: datetime
    updated_at: datetime


class ExpenseWithDetails(ExpenseResponse):
    payer: UserResponse
    splits: list[ExpenseSplitResponse] = []
    confirmations: list[ExpenseConfirmationResponse] = []


class ConfirmExpenseRequest(BaseModel):
    status: str  # 'confirmed' or 'rejected'
