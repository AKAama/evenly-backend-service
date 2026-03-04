from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Query
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models import User
from app.schemas.user import UserResponse, UserUpdate, PasswordChange
from app.utils.deps import get_current_user
from app.services.cos import get_cos_service
from app.services.auth import verify_password, get_password_hash
from app.config import settings

router = APIRouter(prefix="/users", tags=["users"])


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
    if len(contents) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail="File too large. Max size: 5MB"
        )

    # Upload to COS
    cos_service = get_cos_service()
    avatar_url = cos_service.upload_file(
        file_data=contents,
        filename=file.filename,
        folder="avatars"
    )

    # Delete old avatar if exists (optional: implement cleanup)
    # if current_user.avatar_url:
    #     cos_service.delete_file(current_user.avatar_url)

    # Update user
    current_user.avatar_url = avatar_url
    db.commit()
    db.refresh(current_user)

    return current_user


@router.get("/search", response_model=List[UserResponse])
def search_users(
    q: str = Query(..., min_length=1, description="Search query (email or display name)"),
    limit: int = Query(20, le=50, description="Maximum number of results"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Search users by email or display name"""
    # Search by email or display_name (case insensitive)
    query = db.query(User).filter(
        (User.email.ilike(f"%{q}%")) | (User.display_name.ilike(f"%{q}%"))
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
        current_user.display_name = user_update.display_name
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
