from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, EmailStr, ConfigDict, Field
from typing import Literal


# User schemas
class UserBase(BaseModel):
    email: EmailStr
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    username_is_generated: bool = False
    # Preset nameplate key: founder | crew | mate | beta | vip | null
    badge: str | None = None


class UserCreate(UserBase):
    password: str


class UserLogin(BaseModel):
    identifier: str
    password: str


class AppleLoginRequest(BaseModel):
    identity_token: str
    nonce: str
    full_name: str | None = Field(default=None, max_length=100)


class UserResponse(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    # Override EmailStr: deactivated accounts use synthetic placeholders that
    # email-validator rejects (e.g. historical @invalid.local rows).
    email: str
    # app | platform (platform = ops console only).
    account_kind: str = "app"
    # True only for platform ops accounts (account_kind=platform).
    is_admin: bool = False
    # active | deactivated
    status: str = "active"
    # Resolved Chinese label for clients (null when no badge).
    badge_label: str | None = None
    # Ant Design color name or hex for chip styling.
    badge_color: str | None = None
    # Client-facing name; includes （已注销） when deactivated.
    public_display_name: str | None = None


class UserBadgeUpdate(BaseModel):
    """Platform admin assigns or clears a nameplate on a user."""
    badge: str | None = Field(
        default=None,
        description="Badge key from catalog, or null to clear",
    )


class AdminPasswordReset(BaseModel):
    """Platform admin sets a new password for a user (no old password required)."""
    new_password: str = Field(min_length=6, max_length=128)


class BadgeCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=200)
    color: str = Field(default="blue", max_length=32)
    key: str | None = Field(default=None, max_length=32, description="Optional machine key")
    sort_order: int | None = None


class BadgeUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=200)
    color: str | None = Field(default=None, max_length=32)
    sort_order: int | None = None
    is_active: bool | None = None


class BadgeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    key: str
    label: str
    description: str | None = None
    color: str = "blue"
    sort_order: int = 0
    is_active: bool = True
    user_count: int = 0
    created_at: datetime | None = None


class PlatformUserCreate(BaseModel):
    """Create an ops-only console account (not for iOS ledgers)."""

    email: EmailStr
    username: str = Field(min_length=3, max_length=30)
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=100)


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    user_id: UUID | None = None


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    avatar_url: str | None = None


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


class UsernameUpdate(BaseModel):
    username: str = Field(min_length=3, max_length=30)


class PasswordSetup(BaseModel):
    code: str
    new_password: str = Field(min_length=6)


class AuthMethodsResponse(BaseModel):
    methods: list[str]
    has_password: bool


class EmailChange(BaseModel):
    new_email: EmailStr
    code: str
    password: str


class EmailChangeCodeRequest(BaseModel):
    new_email: EmailStr


class PasswordReset(BaseModel):
    email: EmailStr
    code: str
    new_password: str = Field(min_length=6)


class PushDeviceRegistration(BaseModel):
    environment: Literal["sandbox", "production"]
    bundle_id: str = Field(min_length=1, max_length=255)


class DeactivationOwnerTransfer(BaseModel):
    ledger_id: UUID
    new_owner_id: UUID | None = None


class DeactivateAccountRequest(BaseModel):
    owner_transfers: list[DeactivationOwnerTransfer] = Field(default_factory=list)
    confirm: bool = True


class MemberBriefResponse(BaseModel):
    user_id: UUID
    display_name: str
    username: str


class TransferPreviewItemResponse(BaseModel):
    ledger_id: UUID
    ledger_name: str
    member_count_registered_active: int
    default_successor: MemberBriefResponse | None = None
    candidates: list[MemberBriefResponse] = Field(default_factory=list)


class ArchivePreviewItemResponse(BaseModel):
    ledger_id: UUID
    ledger_name: str
    action: str = "archive"
    reason: str = "sole_owner_deactivated"


class DeactivationPreviewResponse(BaseModel):
    owned_ledgers_requiring_transfer: list[TransferPreviewItemResponse]
    owned_ledgers_to_archive: list[ArchivePreviewItemResponse]
    membership_ledger_count: int


class TransferResultResponse(BaseModel):
    ledger_id: UUID
    ledger_name: str
    action: str
    new_owner: MemberBriefResponse | None = None


class DeactivateAccountResponse(BaseModel):
    transfers: list[TransferResultResponse]
