from datetime import datetime, date
from uuid import UUID
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import UserResponse


class ExpenseSplitBase(BaseModel):
    amount: Decimal


class ExpenseSplitCreate(ExpenseSplitBase):
    user_id: UUID | None = None
    member_id: UUID | None = None


class ExpenseSplitResponse(ExpenseSplitBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    expense_id: UUID
    user_id: UUID | None = None
    member_id: UUID | None = None
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


class VoiceExpenseSplitDraft(BaseModel):
    member_id: UUID
    user_id: UUID | None = None
    amount: Decimal


class VoiceExpenseDraft(BaseModel):
    transcript: str
    title: str
    amount: Decimal
    total_amount: Decimal | None = None
    currency: str = "CNY"
    category: str | None = None
    note: str | None = None
    expense_date: date = Field(default_factory=date.today)
    payer_user_id: UUID
    participant_member_ids: list[UUID]
    split_type: str = "equal"
    splits: list[VoiceExpenseSplitDraft] = Field(default_factory=list)
    confidence: float | None = None
    missing_fields: list[str] = Field(default_factory=list)
    confirmation_text: str
