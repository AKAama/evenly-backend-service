from datetime import datetime, timedelta
from uuid import UUID
from jose import JWTError, jwt
import bcrypt
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuthIdentity, User
from app.schemas.user import TokenData, UserCreate, UserLogin


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8")
    )


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.algorithm)
    return encoded_jwt


def decode_token(token: str) -> TokenData | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.algorithm])
        user_id: str = payload.get("sub")
        if user_id is None:
            return None
        return TokenData(user_id=UUID(user_id))
    except JWTError:
        return None


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.query(User).filter(func.lower(User.email) == email.strip().lower()).first()


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.query(User).filter(func.lower(User.username) == username.lower()).first()


def get_user_by_id(db: Session, user_id: UUID) -> User | None:
    return db.query(User).filter(User.id == user_id).first()


def get_password_identity(db: Session, user_id: UUID) -> AuthIdentity | None:
    return db.query(AuthIdentity).filter(
        AuthIdentity.user_id == user_id,
        AuthIdentity.provider == "password",
    ).first()


def set_password(db: Session, user: User, password: str) -> None:
    """Update the password identity and the legacy compatibility column."""
    password_hash = get_password_hash(password)
    identity = get_password_identity(db, user.id)
    if identity is None:
        identity = AuthIdentity(
            user_id=user.id,
            provider="password",
            provider_subject=user.email.strip().lower(),
            email=user.email.strip().lower(),
        )
        db.add(identity)
    identity.password_hash = password_hash
    user.password_hash = password_hash


def change_password_email(db: Session, user: User, new_email: str) -> None:
    """Move the password login identifier while preserving the same account."""
    normalized_email = new_email.strip().lower()
    identity = get_password_identity(db, user.id)
    if identity is None:
        identity = AuthIdentity(
            user_id=user.id,
            provider="password",
            password_hash=user.password_hash,
        )
        db.add(identity)
    identity.provider_subject = normalized_email
    identity.email = normalized_email
    user.email = normalized_email


def create_user(db: Session, user: UserCreate) -> User:
    hashed_password = get_password_hash(user.password)
    db_user = User(
        email=str(user.email).strip().lower(),
        username=user.username,
        password_hash=hashed_password,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        account_kind="app",
        is_admin=False,
    )
    db.add(db_user)
    db.flush()
    db.add(AuthIdentity(
        user_id=db_user.id,
        provider="password",
        provider_subject=str(user.email).strip().lower(),
        email=str(user.email).strip().lower(),
        password_hash=hashed_password,
    ))
    db.commit()
    db.refresh(db_user)
    return db_user


def authenticate_user(db: Session, user_login: UserLogin) -> User | None:
    identifier = user_login.identifier.strip()
    identity = db.query(AuthIdentity).join(User).filter(
        AuthIdentity.provider == "password",
        or_(
            func.lower(AuthIdentity.provider_subject) == identifier.lower(),
            func.lower(User.username) == identifier.lower(),
        ),
    ).first()
    if not identity or not identity.password_hash:
        return None
    if not verify_password(user_login.password, identity.password_hash):
        return None
    return identity.user
