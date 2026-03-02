import logging
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from typing import Annotated
from pydantic import BaseModel, EmailStr

from app.database import get_db
from app.schemas.user import UserCreate, UserResponse, Token
from app.services.auth import create_user, authenticate_user, create_access_token, get_user_by_email
from app.services.cos import get_cos_service
from app.services.verification import send_verification_code, verify_code
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterResponse(UserResponse):
    access_token: str
    token_type: str = "bearer"


class SendCodeRequest(BaseModel):
    email: EmailStr


class VerifyCodeRequest(BaseModel):
    email: EmailStr
    code: str


@router.post("/send-verification")
def send_verification(email: str, db: Session = Depends(get_db)):
    """发送邮箱验证码"""
    # 检查邮箱是否已被注册
    existing_user = get_user_by_email(db, email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该邮箱已被注册"
        )

    # 发送验证码
    success = send_verification_code(email)
    if success:
        return {"message": "验证码已发送"}
    else:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="发送过于频繁，请稍后重试"
        )


@router.post("/verify-code")
def verify(email: str, code: str):
    """验证验证码"""
    if verify_code(email, code):
        return {"valid": True}
    return {"valid": False}


@router.post("/register", response_model=RegisterResponse)
async def register(
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    code: Annotated[str, Form()],
    display_name: Annotated[str | None, Form()] = None,
    avatar: Annotated[UploadFile | None, File()] = None,
    db: Session = Depends(get_db)
):
    # 验证验证码
    if not verify_code(email, code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="验证码错误或已过期"
        )

    # Check if user already exists
    existing_user = get_user_by_email(db, email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    # Handle avatar upload
    avatar_url = None
    if avatar:
        logger.info(f"Avatar uploaded: {avatar.filename}, content_type: {avatar.content_type}")
        if settings.cos:
            try:
                # Validate file type
                allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
                if avatar.content_type not in allowed_types:
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid file type. Allowed: jpeg, png, gif, webp"
                    )

                # Read file
                contents = await avatar.read()
                if len(contents) > 5 * 1024 * 1024:
                    raise HTTPException(
                        status_code=400,
                        detail="File too large. Max size: 5MB"
                    )

                logger.info(f"Uploading avatar to COS: {avatar.filename}")

                # Upload to COS
                cos_service = get_cos_service()
                if cos_service is None:
                    logger.error("COS service is None")
                    raise HTTPException(
                        status_code=500,
                        detail="COS service not available"
                    )

                avatar_url = cos_service.upload_file(
                    file_data=contents,
                    filename=avatar.filename,
                    folder="avatars"
                )
                logger.info(f"Avatar uploaded successfully: {avatar_url}")
            except Exception as e:
                logger.error(f"Failed to upload avatar: {str(e)}")
                # Continue without avatar if upload fails
                avatar_url = None
        else:
            logger.warning("COS not configured, skipping avatar upload")

    # Create user
    user_data = UserCreate(
        email=email,
        password=password,
        display_name=display_name,
        avatar_url=avatar_url
    )
    db_user = create_user(db, user_data)
    logger.info(f"User created: {db_user.email}, avatar_url: {db_user.avatar_url}")

    # Generate token immediately
    access_token = create_access_token(
        data={"sub": str(db_user.id)},
        expires_delta=timedelta(minutes=settings.jwt_expire_minutes)
    )

    # Convert to response model
    user_response = UserResponse.model_validate(db_user)
    return RegisterResponse(
        **user_response.model_dump(),
        access_token=access_token
    )


@router.post("/login", response_model=Token)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    # Use form_data.username as email
    user = authenticate_user(db, type("UserLogin", (), {"email": form_data.username, "password": form_data.password})())

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(
        data={"sub": str(user.id)},
        expires_delta=timedelta(minutes=settings.jwt_expire_minutes)
    )

    return {"access_token": access_token, "token_type": "bearer"}
