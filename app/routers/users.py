from uuid import UUID
import logging
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
)
from app.schemas.user import UserResponse, UserUpdate, PasswordChange, EmailChange, EmailChangeCodeRequest
from app.utils.deps import get_current_user
from app.services.cos import get_cos_service
from app.services.auth import verify_password, get_password_hash, get_user_by_email
from app.services.verification import send_verification_code, verify_code
from app.config import settings

router = APIRouter(prefix="/users", tags=["users"])
logger = logging.getLogger(__name__)


@router.get("/me", response_model=UserResponse)
def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Get current user information"""
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
    # Search by email, username, or display_name (case insensitive)
    query = db.query(User).filter(
        (User.email.ilike(f"%{q}%")) |
        (User.username.ilike(f"%{q}%")) |
        (User.display_name.ilike(f"%{q}%"))
    ).limit(limit).all()

    # Exclude current user from results
    return [u for u in query if u.id != current_user.id]


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
    return current_user


@router.put("/me/password")
def change_password(
    password_change: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Change user password"""
    # Verify old password
    if not verify_password(password_change.old_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect old password"
        )

    # Update password
    current_user.password_hash = get_password_hash(password_change.new_password)
    db.commit()

    return {"message": "Password updated successfully"}


@router.post("/me/email/send-verification")
def send_email_change_code(
    request: EmailChangeCodeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
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
    if not verify_password(request.password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码错误")
    if get_user_by_email(db, new_email):
        raise HTTPException(status_code=400, detail="该邮箱已被使用")
    if not verify_code(new_email, request.code, purpose="email_change"):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    current_user.email = new_email
    db.commit()
    db.refresh(current_user)
    return current_user


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Permanently delete the current account and its associated data."""
    user_id = current_user.id
    avatar_url = current_user.avatar_url

    # Delete ledgers owned by the user in full.
    for ledger in db.query(Ledger).filter(Ledger.owner_id == user_id).all():
        db.delete(ledger)
    db.flush()

    # Remove user-generated records from shared ledgers. These records retain
    # direct references to the account and must not survive account deletion.
    split_expense_ids = {
        expense_id
        for (expense_id,) in db.query(ExpenseSplit.expense_id).filter(
            ExpenseSplit.user_id == user_id
        ).all()
    }
    related_expenses = db.query(Expense).filter(
        (Expense.payer_id == user_id)
        | (Expense.created_by == user_id)
        | (Expense.id.in_(split_expense_ids) if split_expense_ids else False)
    ).all()
    for expense in related_expenses:
        db.delete(expense)
    db.flush()

    db.query(Settlement).filter(
        (Settlement.from_user_id == user_id) | (Settlement.to_user_id == user_id)
    ).delete(synchronize_session=False)
    db.query(ExpenseConfirmation).filter(
        ExpenseConfirmation.user_id == user_id
    ).delete(synchronize_session=False)
    db.query(ExpenseSplit).filter(
        ExpenseSplit.user_id == user_id
    ).delete(synchronize_session=False)
    db.query(LedgerMember).filter(
        LedgerMember.user_id == user_id
    ).delete(synchronize_session=False)

    db.delete(current_user)
    db.commit()

    if avatar_url and settings.cos:
        try:
            cos_service = get_cos_service()
            if cos_service is not None:
                cos_service.delete_file(avatar_url)
        except Exception:
            # Do not restore a deleted account because external object cleanup
            # failed. The orphan can be cleaned up from logs later.
            logger.exception("Avatar cleanup failed for deleted user_id=%s", user_id)

    logger.info("Account permanently deleted user_id=%s", user_id)
    return None
