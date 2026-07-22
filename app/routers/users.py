from uuid import UUID
from datetime import datetime
import logging
import re
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Query
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models import (
    User,
    Ledger,
    LedgerMember,
    Expense,
    ExpenseSplit,
    ExpenseConfirmation,
    Settlement,
    AuthIdentity,
    PushDevice,
)
from app.schemas.user import (
    AuthMethodsResponse,
    ArchivePreviewItemResponse,
    DeactivateAccountRequest,
    DeactivateAccountResponse,
    DeactivationPreviewResponse,
    EmailChange,
    EmailChangeCodeRequest,
    MemberBriefResponse,
    PasswordChange,
    PasswordSetup,
    TransferPreviewItemResponse,
    TransferResultResponse,
    UsernameUpdate,
    UserResponse,
    UserUpdate,
    PushDeviceRegistration,
)
from app.utils.deps import get_current_user
from app.services.cos import get_cos_service
from app.services.auth import (
    change_password_email,
    get_password_identity,
    get_user_by_email,
    set_password,
    get_user_by_username,
    verify_password,
)
from app.services import deactivation as deactivation_service
from app.services.verification import send_verification_code, verify_code
from app.services.rate_limit import enforce_rate_limit
from app.config import settings

router = APIRouter(prefix="/users", tags=["users"])
logger = logging.getLogger(__name__)
PUSH_TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F]{64,200}$")


@router.put("/me/push-devices/{token}", status_code=status.HTTP_204_NO_CONTENT)
def register_push_device(
    token: str,
    request: PushDeviceRegistration,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not PUSH_TOKEN_PATTERN.fullmatch(token):
        raise HTTPException(status_code=422, detail="Invalid APNs device token")
    normalized = token.lower()
    device = db.query(PushDevice).filter(PushDevice.token == normalized).first()
    if device is None:
        device = PushDevice(token=normalized, user_id=current_user.id)
        db.add(device)
    device.user_id = current_user.id
    device.environment = request.environment
    device.bundle_id = request.bundle_id
    device.is_active = True
    device.last_seen_at = datetime.utcnow()
    db.commit()


@router.delete("/me/push-devices/{token}", status_code=status.HTTP_204_NO_CONTENT)
def delete_push_device(
    token: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    device = db.query(PushDevice).filter(
        PushDevice.token == token.lower(),
        PushDevice.user_id == current_user.id,
    ).first()
    if device is not None:
        device.is_active = False
        db.commit()


@router.get("/me", response_model=UserResponse)
def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user information"""
    from app.services.audit import user_to_response

    return user_to_response(current_user)


@router.get("/me/auth-methods", response_model=AuthMethodsResponse)
def get_auth_methods(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    methods = [
        provider
        for (provider,) in db.query(AuthIdentity.provider)
        .filter(AuthIdentity.user_id == current_user.id)
        .order_by(AuthIdentity.provider)
        .all()
    ]
    return AuthMethodsResponse(methods=methods, has_password="password" in methods)


@router.put("/me/username", response_model=UserResponse)
def update_username(
    request: UsernameUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    username = request.username.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,29}", username):
        raise HTTPException(status_code=400, detail="用户名须为3-30位，以英文字母开头，仅包含英文、数字和下划线")
    if username.lower() != (current_user.username or "").lower():
        deactivation_service.ensure_username_available(db, username)
    current_user.username = username
    current_user.username_is_generated = False
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/me/avatar", response_model=UserResponse)
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Upload user avatar"""
    # Check if COS is configured
    if not settings.cos:
        raise HTTPException(
            status_code=503,
            detail="Avatar upload not configured"
        )

    # Validate file type
    allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Allowed: jpeg, png, gif, webp"
        )

    # Validate file size (max 5MB)
    contents = await file.read()
    logger.info(
        "Avatar upload received user_id=%s filename=%s content_type=%s size=%d",
        current_user.id,
        file.filename,
        file.content_type,
        len(contents),
    )
    if len(contents) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail="File too large. Max size: 5MB"
        )

    # Upload to COS
    try:
        cos_service = get_cos_service()
        if cos_service is None:
            raise RuntimeError("COS service unavailable")
        avatar_url = cos_service.upload_file(
            file_data=contents,
            filename=file.filename,
            folder="avatars"
        )
    except Exception as exc:
        logger.exception("Avatar storage failed user_id=%s: %s", current_user.id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Avatar storage is temporarily unavailable",
        ) from exc

    # Delete old avatar if exists (optional: implement cleanup)
    # if current_user.avatar_url:
    #     cos_service.delete_file(current_user.avatar_url)

    # Update user
    current_user.avatar_url = avatar_url
    db.commit()
    db.refresh(current_user)

    logger.info("Avatar updated user_id=%s", current_user.id)

    return current_user


@router.get("/search", response_model=List[UserResponse])
def search_users(
    q: str = Query(..., min_length=1, description="Search query (email or display name)"),
    limit: int = Query(20, le=50, description="Maximum number of results"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Search users by email or display name"""
    enforce_rate_limit(
        f"search:user:{current_user.id}",
        limit=60,
        window_seconds=60,
        detail="搜索过于频繁，请稍后重试",
    )
    # Search by email, username, or display_name (case insensitive); hide deactivated.
    query = db.query(User).filter(
        ((User.status == "active") | (User.status.is_(None))),
        (User.email.ilike(f"%{q}%"))
        | (User.username.ilike(f"%{q}%"))
        | (User.display_name.ilike(f"%{q}%")),
    ).limit(limit).all()

    # Exclude current user from results
    from app.services.audit import user_to_response

    return [user_to_response(u) for u in query if u.id != current_user.id]


@router.put("/me", response_model=UserResponse)
def update_user_info(
    user_update: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update current user information (display_name)"""
    if user_update.display_name is not None:
        display_name = user_update.display_name.strip()
        if not display_name:
            raise HTTPException(status_code=400, detail="Display name cannot be blank")
        current_user.display_name = display_name
    if user_update.avatar_url is not None:
        current_user.avatar_url = user_update.avatar_url

    db.commit()
    db.refresh(current_user)
    from app.services.audit import user_to_response

    return user_to_response(current_user)


@router.put("/me/password")
def change_password(
    password_change: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Change user password (app users and platform ops)."""
    # Verify old password
    identity = get_password_identity(db, current_user.id)
    if not identity or not identity.password_hash or not verify_password(
        password_change.old_password, identity.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前密码不正确",
        )

    if len(password_change.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")

    # Update password
    set_password(db, current_user, password_change.new_password)
    db.commit()

    from app.services.audit import record_audit

    record_audit(
        db,
        action="auth.password_change",
        actor=current_user,
        resource_type="user",
        resource_id=current_user.id,
        summary=f"修改密码 {current_user.username}",
        source="console",
    )

    return {"message": "密码已更新"}


@router.post("/me/password/setup/send")
def send_password_setup_code(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(
        f"send_code:user:{current_user.id}",
        limit=10,
        window_seconds=3600,
        detail="发送过于频繁，请稍后重试",
    )
    if get_password_identity(db, current_user.id):
        raise HTTPException(status_code=400, detail="账号已经设置密码")
    if not send_verification_code(current_user.email, purpose="password_setup"):
        raise HTTPException(status_code=429, detail="发送过于频繁，请稍后重试")
    return {"message": "验证码已发送"}


@router.put("/me/password/setup")
def setup_password(
    request: PasswordSetup,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if get_password_identity(db, current_user.id):
        raise HTTPException(status_code=400, detail="账号已经设置密码")
    if not verify_code(current_user.email, request.code, purpose="password_setup"):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    set_password(db, current_user, request.new_password)
    db.commit()
    return {"message": "密码设置成功"}


@router.post("/me/email/send-verification")
def send_email_change_code(
    request: EmailChangeCodeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(
        f"send_code:user:{current_user.id}",
        limit=10,
        window_seconds=3600,
        detail="发送过于频繁，请稍后重试",
    )
    new_email = str(request.new_email).strip().lower()
    if new_email == current_user.email.lower():
        raise HTTPException(status_code=400, detail="新邮箱不能与当前邮箱相同")
    if get_user_by_email(db, new_email):
        raise HTTPException(status_code=400, detail="该邮箱已被使用")
    if not send_verification_code(new_email, purpose="email_change"):
        raise HTTPException(status_code=429, detail="发送过于频繁，请稍后重试")
    return {"message": "验证码已发送"}


@router.put("/me/email", response_model=UserResponse)
def change_email(
    request: EmailChange,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    new_email = str(request.new_email).strip().lower()
    identity = get_password_identity(db, current_user.id)
    if not identity or not identity.password_hash or not verify_password(
        request.password, identity.password_hash
    ):
        raise HTTPException(status_code=400, detail="当前密码错误")
    if get_user_by_email(db, new_email):
        raise HTTPException(status_code=400, detail="该邮箱已被使用")
    if not verify_code(new_email, request.code, purpose="email_change"):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    change_password_email(db, current_user, new_email)
    db.commit()
    db.refresh(current_user)
    return current_user


@router.get("/me/deactivation-preview", response_model=DeactivationPreviewResponse)
def deactivation_preview(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Preview owner transfers and archives before account deactivation."""
    preview = deactivation_service.build_preview(db, current_user)

    def _brief(b) -> MemberBriefResponse | None:
        if b is None:
            return None
        return MemberBriefResponse(
            user_id=b.user_id,
            display_name=b.display_name,
            username=b.username,
        )

    return DeactivationPreviewResponse(
        owned_ledgers_requiring_transfer=[
            TransferPreviewItemResponse(
                ledger_id=item.ledger_id,
                ledger_name=item.ledger_name,
                member_count_registered_active=item.member_count_registered_active,
                default_successor=_brief(item.default_successor),
                candidates=[
                    MemberBriefResponse(
                        user_id=c.user_id,
                        display_name=c.display_name,
                        username=c.username,
                    )
                    for c in item.candidates
                ],
            )
            for item in preview.owned_ledgers_requiring_transfer
        ],
        owned_ledgers_to_archive=[
            ArchivePreviewItemResponse(
                ledger_id=item.ledger_id,
                ledger_name=item.ledger_name,
                action=item.action,
                reason=item.reason,
            )
            for item in preview.owned_ledgers_to_archive
        ],
        membership_ledger_count=preview.membership_ledger_count,
    )


@router.post("/me/deactivate", response_model=DeactivateAccountResponse)
def deactivate_account(
    body: DeactivateAccountRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Soft-deactivate account: transfer/archive owned ledgers, keep shared history."""
    payload = body or DeactivateAccountRequest(confirm=True)
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="请确认注销")
    results = deactivation_service.deactivate_user(
        db,
        current_user,
        owner_transfers=[t.model_dump() for t in payload.owner_transfers],
        actor=current_user,
        admin=False,
    )
    return DeactivateAccountResponse(
        transfers=[
            TransferResultResponse(
                ledger_id=r.ledger_id,
                ledger_name=r.ledger_name,
                action=r.action,
                new_owner=(
                    MemberBriefResponse(
                        user_id=r.new_owner.user_id,
                        display_name=r.new_owner.display_name,
                        username=r.new_owner.username,
                    )
                    if r.new_owner
                    else None
                ),
            )
            for r in results
        ]
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Deprecated hard-delete path: performs the same soft deactivation as POST /me/deactivate."""
    deactivation_service.deactivate_user(
        db,
        current_user,
        owner_transfers=[],
        actor=current_user,
        admin=False,
    )
    return None
