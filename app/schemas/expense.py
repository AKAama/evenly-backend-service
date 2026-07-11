from datetime import datetime, date
from uuid import UUID
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.user import UserResponse


ALLOWED_EXPENSE_SF_SYMBOLS = {
    "fork.knife", "sunrise.fill", "sun.max.fill", "moon.stars.fill",
    "cup.and.saucer.fill", "takeoutbag.and.cup.and.straw.fill",
    "car.fill", "train.side.front.car", "tram.fill", "airplane",
    "fuelpump.fill", "bed.double.fill", "building.2.fill", "house.fill",
    "ticket.fill", "cart.fill", "bag.fill", "gift.fill", "gamecontroller.fill",
    "film.fill", "music.note", "cross.case.fill", "pills.fill", "book.fill",
    "graduationcap.fill", "pawprint.fill", "figure.walk", "dumbbell.fill",
    "ellipsis.circle.fill",
}
ALLOWED_EXPENSE_EMOJIS = {
    "🍜", "🍚", "🍔", "🍕", "☕️", "🍺", "🚕", "🚄", "🚇", "✈️",
    "⛽️", "🏨", "🏠", "🎫", "🛒", "🛍️", "🎁", "🎮", "🎬", "🎵",
    "🏥", "💊", "📚", "🎓", "🐾", "🏃", "🏋️", "💡", "💰", "🧾",
}


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
    category: str | None = Field(default=None, max_length=50)
    icon_type: Literal["sf_symbol", "emoji"] | None = None
    icon_value: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_icon(self):
        if self.icon_type is None and self.icon_value is None:
            return self
        if self.icon_type is None or self.icon_value is None:
            raise ValueError("icon_type and icon_value must be provided together")
        allowed = ALLOWED_EXPENSE_SF_SYMBOLS if self.icon_type == "sf_symbol" else ALLOWED_EXPENSE_EMOJIS
        if self.icon_value not in allowed:
            raise ValueError("Unsupported expense icon")
        return self


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


def expense_to_with_details(
    expense,
    *,
    status: str | None = None,
    payer=None,
    splits=None,
    confirmations=None,
) -> ExpenseWithDetails:
    """Build list/detail payload from an Expense ORM row.

    Manual constructors historically omitted category/icon fields, so the app
    always fell back to the default ¥ icon even when icons were persisted.
    """
    base = ExpenseResponse.model_validate(expense).model_dump()
    if status is not None:
        base["status"] = status

    payer_obj = payer if payer is not None else expense.payer
    split_src = splits if splits is not None else expense.splits
    conf_src = confirmations if confirmations is not None else expense.confirmations

    return ExpenseWithDetails(
        **base,
        payer=UserResponse.model_validate(payer_obj),
        splits=[
            s if isinstance(s, ExpenseSplitResponse) else ExpenseSplitResponse.model_validate(s)
            for s in split_src
        ],
        confirmations=[
            c
            if isinstance(c, ExpenseConfirmationResponse)
            else ExpenseConfirmationResponse.model_validate(c)
            for c in conf_src
        ],
    )


class ConfirmExpenseRequest(BaseModel):
    status: str  # 'confirmed' or 'rejected'


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
    splits: list[dict] = Field(default_factory=list)
    confidence: float | None = None
    missing_fields: list[str] = Field(default_factory=list)
    confirmation_text: str
