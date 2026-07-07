from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict

from app.schemas.user import UserResponse
from app.schemas.expense import ExpenseWithDetails
from app.schemas.settlement import SettlementInstruction, SettlementWithUsers


class LedgerMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID | None = None
    nickname: str | None = None
    joined_at: datetime
    is_temporary: bool = False
    temporary_name: str | None = None
    status: str = "active"


class LedgerMemberWithUser(LedgerMemberResponse):
    user: UserResponse | None = None


class LedgerBase(BaseModel):
    name: str
    currency: str = "CNY"


class MemberCreate(BaseModel):
    """Member to add during ledger creation"""
    user_id: UUID | None = None
    nickname: str | None = None
    is_temporary: bool = False
    temporary_name: str | None = None


class LedgerCreate(LedgerBase):
    members: list[MemberCreate] = []


class LedgerResponse(LedgerBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    created_at: datetime
    updated_at: datetime
    member_count: int = 0
    expense_count: int = 0


class LedgerWithMembers(LedgerResponse):
    members: list[LedgerMemberWithUser] = []


class AddMemberRequest(BaseModel):
    user_id: UUID | None = None  # None for temporary members
    nickname: str | None = None
    is_temporary: bool = False
    temporary_name: str | None = None  # Required if is_temporary is True


class MemberResponse(BaseModel):
    id: UUID | None = None
    user_id: UUID | None = None  # None for temporary members
    nickname: str | None = None
    joined_at: datetime
    user: UserResponse | None = None  # None for temporary members
    is_temporary: bool = False
    temporary_name: str | None = None
    status: str = "active"


class LedgerInvitationResponse(BaseModel):
    id: UUID
    ledger_id: UUID
    ledger_name: str
    invited_by_name: str
    created_at: datetime


class LedgerOverviewResponse(BaseModel):
    ledger: LedgerWithMembers
    expenses: list[ExpenseWithDetails]
    settlement_suggestions: list[SettlementInstruction]
    settlement_history: list[SettlementWithUsers]
