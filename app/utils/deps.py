from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Ledger, LedgerMember, User
from app.services.auth import decode_token, get_user_by_id

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def get_current_user(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    access_token = token or request.cookies.get(settings.auth_cookie_name)
    if not access_token:
        raise credentials_exception

    token_data = decode_token(access_token)
    if token_data is None or token_data.user_id is None:
        raise credentials_exception

    user = get_user_by_id(db, token_data.user_id)
    if user is None:
        raise credentials_exception

    return user


def get_current_user_optional(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> User | None:
    """Optional authentication - returns None if token is invalid or not provided"""
    try:
        return get_current_user(request, token, db)
    except HTTPException:
        return None


def get_ledger_or_404(db: Session, ledger_id) -> Ledger:
    ledger = db.query(Ledger).filter(Ledger.id == ledger_id).first()
    if not ledger:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ledger not found")
    return ledger


def require_ledger_member(db: Session, ledger_id, current_user: User) -> LedgerMember:
    membership = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.user_id == current_user.id,
        LedgerMember.is_temporary.is_(False),
    ).first()

    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of this ledger"
        )

    return membership


def require_ledger_owner(db: Session, ledger_id, current_user: User) -> Ledger:
    ledger = get_ledger_or_404(db, ledger_id)
    if ledger.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owner can perform this action"
        )
    return ledger
