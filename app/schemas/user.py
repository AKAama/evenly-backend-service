from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, EmailStr, ConfigDict, Field


# User schemas
class UserBase(BaseModel):
    email: EmailStr
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    username_is_generated: bool = False


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
