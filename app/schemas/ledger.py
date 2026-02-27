from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict

from app.schemas.user import UserResponse


class LedgerMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    nickname: str | None
    joined_at: datetime


class LedgerMemberWithUser(LedgerMemberResponse):
    user: UserResponse


class LedgerBase(BaseModel):
    name: str
    currency: str = "CNY"


class LedgerCreate(LedgerBase):
    pass


class LedgerResponse(LedgerBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    created_at: datetime
    updated_at: datetime


class LedgerWithMembers(LedgerResponse):
    members: list[LedgerMemberWithUser] = []


class AddMemberRequest(BaseModel):
    user_id: UUID
    nickname: str | None = None


class MemberResponse(BaseModel):
    user_id: UUID
    nickname: str | None
    joined_at: datetime
    user: UserResponse
