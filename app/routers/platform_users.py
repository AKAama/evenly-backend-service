"""Platform ops accounts: console-only, no ledger membership."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuthIdentity, User
from app.schemas.user import PlatformUserCreate, UserResponse
from app.services.audit import is_user_admin, record_audit, user_to_response
from app.services.auth import get_password_hash, get_user_by_email, get_user_by_username
from app.utils.deps import get_current_user

router = APIRouter(prefix="/admin/platform-users", tags=["platform-users"])


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not is_user_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def create_platform_user_record(db: Session, body: PlatformUserCreate) -> User:
    email = body.email.strip().lower()
    username = body.username.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,29}", username):
        raise HTTPException(
            status_code=400,
            detail="用户名须为3-30位，以英文字母开头，仅包含英文、数字和下划线",
        )
    if get_user_by_email(db, email):
        raise HTTPException(status_code=400, detail="邮箱已被注册")
    if get_user_by_username(db, username):
        raise HTTPException(status_code=400, detail="用户名已被使用")

    password_hash = get_password_hash(body.password)
    user = User(
        email=email,
        username=username,
        username_is_generated=False,
        password_hash=password_hash,
        display_name=(body.display_name or username).strip(),
        account_kind="platform",
        is_admin=True,
    )
    db.add(user)
    db.flush()
    db.add(
        AuthIdentity(
            user_id=user.id,
            provider="password",
            provider_subject=email,
            email=email,
            password_hash=password_hash,
        )
    )
    db.commit()
    db.refresh(user)
    return user


@router.get("", response_model=list[UserResponse])
def list_platform_users(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    rows = (
        db.query(User)
        .filter(User.account_kind == "platform")
        .order_by(User.created_at.desc())
        .all()
    )
    return [user_to_response(u) for u in rows]


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_platform_user(
    body: PlatformUserCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Create a pure platform ops account (console only)."""
    user = create_platform_user_record(db, body)
    record_audit(
        db,
        action="platform_user.create",
        actor=admin,
        resource_type="user",
        resource_id=user.id,
        summary=f"创建平台账号 {user.username} ({user.email})",
        source="console",
    )
    return user_to_response(user)
