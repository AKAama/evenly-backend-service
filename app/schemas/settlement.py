from datetime import datetime
from uuid import UUID
from decimal import Decimal
from pydantic import BaseModel, ConfigDict

from app.schemas.user import UserResponse


class SettlementBase(BaseModel):
    amount: Decimal
    note: str | None = None


class SettlementCreate(SettlementBase):
    from_user_id: UUID
    to_user_id: UUID


class SettlementResponse(SettlementBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ledger_id: UUID
    from_user_id: UUID
    to_user_id: UUID
    settled_at: datetime


class SettlementWithUsers(SettlementResponse):
    from_user: UserResponse
    to_user: UserResponse


class SettlementInstruction(BaseModel):
    """Represents a calculated settlement instruction"""
    from_user_id: UUID
    from_user_name: str
    to_user_id: UUID
    to_user_name: str
    amount: Decimal
