import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.schemas.user import UserCreate, UserResponse
from app.services.auth import create_user, get_user_by_email, get_user_by_username


router = APIRouter(prefix="/test", tags=["test"])


class CreateTestUserRequest(BaseModel):
    email: EmailStr
    username: str = Field(
        min_length=3,
        max_length=30,
        pattern=r"^[A-Za-z][A-Za-z0-9_]{2,29}$",
    )
    password: str = Field(min_length=6)
    display_name: str | None = Field(default=None, max_length=100)


def require_test_admin_token(
    provided_token: Annotated[
        str | None,
        Header(alias="X-Test-Admin-Token"),
    ] = None,
) -> None:
    configured_token = settings.test_admin_token
    if not configured_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if provided_token is None or not secrets.compare_digest(provided_token, configured_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_test_admin_token)],
)
def create_test_user(request: CreateTestUserRequest, db: Session = Depends(get_db)):
    if get_user_by_email(db, str(request.email)):
        raise HTTPException(status_code=400, detail="Email already registered")
    if get_user_by_username(db, request.username):
        raise HTTPException(status_code=400, detail="Username already registered")

    return create_user(
        db,
        UserCreate(
            email=request.email,
            username=request.username,
            password=request.password,
            display_name=request.display_name,
        ),
    )
