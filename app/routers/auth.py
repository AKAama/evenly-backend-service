import logging
import re
import secrets
import hashlib
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status, UploadFile, File, Form
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from typing import Annotated
from pydantic import BaseModel, EmailStr

from app.database import get_db
from app.models import AuthIdentity, User
from app.schemas.user import AppleLoginRequest, UserCreate, UserResponse, Token, PasswordReset
from app.services.auth import create_user, authenticate_user, create_access_token, get_password_hash, get_user_by_email, get_user_by_username, set_password
from app.services.apple_auth import AppleTokenError, verify_apple_identity_token
from app.services.cos import get_cos_service
from app.services.rate_limit import client_ip, enforce_rate_limit
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


def set_auth_cookie(response: Response, access_token: str) -> None:
    max_age = settings.jwt_expire_minutes * 60
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=access_token,
        max_age=max_age,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )


@router.post("/send-verification")
def send_verification(email: str, request: Request, db: Session = Depends(get_db)):
    """发送邮箱验证码"""
    ip = client_ip(request)
    enforce_rate_limit(f"send_code:ip:{ip}", limit=30, window_seconds=3600)
    enforce_rate_limit(
        f"send_code:email:{email.strip().lower()}",
        limit=10,
        window_seconds=3600,
        detail="发送过于频繁，请稍后重试",
    )
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
    request: Request,
    response: Response,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    code: Annotated[str, Form()],
    username: Annotated[str, Form()],
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

    username = username.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,29}", username):
        raise HTTPException(status_code=400, detail="用户名须为3-30位，以英文字母开头，仅包含英文、数字和下划线")
    from app.services.deactivation import ensure_username_available

    ensure_username_available(db, username)

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
        username=username,
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
    set_auth_cookie(response, access_token)

    from app.services.audit import record_audit, user_to_response

    user_response = user_to_response(db_user)
    record_audit(
        db,
        action="auth.register",
        actor=db_user,
        resource_type="user",
        resource_id=db_user.id,
        summary=f"注册 {db_user.username}",
        request=request,
    )
    return RegisterResponse(
        **user_response.model_dump(),
        access_token=access_token
    )


@router.post("/login", response_model=Token)
def login(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    ip = client_ip(request)
    identifier = (form_data.username or "").strip().lower()
    enforce_rate_limit(f"login:ip:{ip}", limit=20, window_seconds=600)
    if identifier:
        enforce_rate_limit(
            f"login:id:{identifier}",
            limit=10,
            window_seconds=600,
            detail="登录尝试过多，请稍后重试",
        )
    # Use form_data.username as email
    user = authenticate_user(db, type("UserLogin", (), {"identifier": form_data.username, "password": form_data.password})())

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
    set_auth_cookie(response, access_token)

    from app.services.audit import record_audit

    is_platform = getattr(user, "account_kind", None) == "platform"
    record_audit(
        db,
        action="auth.login",
        actor=user,
        resource_type="user",
        resource_id=user.id,
        summary=f"登录 {user.username}" + ("（平台账号）" if is_platform else ""),
        request=request,
    )

    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/apple", response_model=Token)
def login_with_apple(
    request: AppleLoginRequest,
    response: Response,
    http_request: Request,
    db: Session = Depends(get_db),
):
    ip = client_ip(http_request)
    enforce_rate_limit(f"login:apple:ip:{ip}", limit=30, window_seconds=600)
    try:
        claims = verify_apple_identity_token(request.identity_token, request.nonce)
    except AppleTokenError as exc:
        raise HTTPException(status_code=401, detail="Apple 登录凭证无效") from exc

    subject = str(claims["sub"])
    identity = db.query(AuthIdentity).filter(
        AuthIdentity.provider == "apple",
        AuthIdentity.provider_subject == subject,
    ).first()

    if identity is not None:
        user = identity.user
        if user is None or (getattr(user, "status", None) or "active") == "deactivated":
            # Soft-deactivated: identities should be gone; treat as invalid credentials.
            raise HTTPException(status_code=401, detail="Apple 登录凭证无效")
        # First Apple sign-in is the only time full_name is usually provided; backfill if empty.
        if request.full_name and request.full_name.strip():
            if not (user.display_name or "").strip() or user.display_name == (user.email or "").split("@", 1)[0]:
                user.display_name = request.full_name.strip()
                db.commit()
                db.refresh(user)
    else:
        # Prefer email from the identity token (includes Hide My Email relay).
        # Never prompt the client to re-enter name/email (App Store SIWA rules).
        email = str(claims.get("email") or "").strip().lower()
        is_placeholder_email = False
        if not email:
            # Rare: token without email claim. Stable placeholder — do not ask the user.
            email = f"apple.{subject[:32].lower()}@privaterelay.evenly.local"
            is_placeholder_email = True

        # Link to an existing account only when Apple gave a real email claim.
        user = None if is_placeholder_email else get_user_by_email(db, email)
        if user is None:
            username_base = f"apple_{hashlib.sha256(subject.encode()).hexdigest()[:12]}"
            username = username_base[:30]
            suffix = 1
            while get_user_by_username(db, username):
                suffix += 1
                username = f"{username_base[:25]}_{suffix}"
            display = (request.full_name or "").strip()
            if not display:
                local = email.split("@", 1)[0]
                display = (
                    "Evenly 用户"
                    if is_placeholder_email or local.startswith("apple_")
                    else local
                )
            user = User(
                email=email,
                username=username,
                username_is_generated=True,
                password_hash=get_password_hash(secrets.token_urlsafe(32)),
                display_name=display,
            )
            db.add(user)
            db.flush()

        db.add(AuthIdentity(
            user_id=user.id,
            provider="apple",
            provider_subject=subject,
            email=None if is_placeholder_email else email,
        ))
        db.commit()
        db.refresh(user)

    access_token = create_access_token(
        data={"sub": str(user.id)},
        expires_delta=timedelta(minutes=settings.jwt_expire_minutes),
    )
    set_auth_cookie(response, access_token)
    from app.services.audit import record_audit

    record_audit(
        db,
        action="auth.apple_login",
        actor=user,
        resource_type="user",
        resource_id=user.id,
        summary=f"Apple 登录 {user.username}",
        request=http_request,
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/logout")
def logout(response: Response):
    clear_auth_cookie(response)
    return {"message": "Logged out"}


@router.post("/password-reset/send")
def send_password_reset_code(
    request: SendCodeRequest,
    http_request: Request,
    db: Session = Depends(get_db),
):
    ip = client_ip(http_request)
    enforce_rate_limit(f"send_code:ip:{ip}", limit=30, window_seconds=3600)
    # Always return the same response to avoid revealing registered emails.
    if get_user_by_email(db, request.email):
        if not send_verification_code(request.email, purpose="password_reset"):
            raise HTTPException(status_code=429, detail="发送过于频繁，请稍后重试")
    return {"message": "如果该邮箱已注册，验证码将发送至邮箱"}


@router.post("/password-reset")
def reset_password(request: PasswordReset, db: Session = Depends(get_db)):
    user = get_user_by_email(db, request.email)
    if not user or not verify_code(request.email, request.code, purpose="password_reset"):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    set_password(db, user, request.new_password)
    db.commit()
    return {"message": "密码已重置"}
