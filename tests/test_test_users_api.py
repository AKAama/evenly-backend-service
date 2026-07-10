import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db
from app.models import AuthIdentity, User
from app.schemas.user import UserLogin
from app.services.auth import authenticate_user
from main import app


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def client(db):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def configured_test_token(monkeypatch):
    monkeypatch.setitem(settings.__dict__, "test_admin_token", "local-test-secret")


def create_test_user(client, **overrides):
    payload = {
        "email": "test001@example.com",
        "username": "test001",
        "password": "secret123",
        "display_name": "Test User",
    }
    payload.update(overrides)
    return client.post(
        "/test/users",
        headers={"X-Test-Admin-Token": "local-test-secret"},
        json=payload,
    )


def test_matching_token_creates_login_user_without_verification_or_session(client, db, monkeypatch):
    def fail_if_called(*args, **kwargs):
        pytest.fail("email verification must not be called")

    monkeypatch.setattr("app.services.verification.verify_code", fail_if_called)
    monkeypatch.setattr("app.services.verification.send_verification_code", fail_if_called)

    response = create_test_user(client)

    assert response.status_code == 201
    assert "access_token" not in response.json()
    assert "set-cookie" not in response.headers
    user = db.query(User).filter(User.email == "test001@example.com").one()
    assert db.query(AuthIdentity).filter_by(user_id=user.id, provider="password").one()
    assert authenticate_user(db, UserLogin(identifier=user.email, password="secret123")) == user


@pytest.mark.parametrize("headers", [{}, {"X-Test-Admin-Token": "wrong-secret"}])
def test_missing_or_wrong_token_is_forbidden(client, headers):
    response = client.post(
        "/test/users",
        headers=headers,
        json={
            "email": "test001@example.com",
            "username": "test001",
            "password": "secret123",
        },
    )

    assert response.status_code == 403


def test_unconfigured_token_hides_endpoint(client, monkeypatch):
    monkeypatch.setitem(settings.__dict__, "test_admin_token", None)

    response = create_test_user(client)

    assert response.status_code == 404


def test_duplicate_email_is_rejected(client):
    assert create_test_user(client).status_code == 201

    response = create_test_user(client, username="another001")

    assert response.status_code == 400
    assert response.json()["detail"] == "Email already registered"


def test_duplicate_username_is_rejected_case_insensitively(client):
    assert create_test_user(client).status_code == 201

    response = create_test_user(client, email="another@example.com", username="TEST001")

    assert response.status_code == 400
    assert response.json()["detail"] == "Username already registered"


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"email": "not-an-email"}, "email"),
        ({"username": "1invalid"}, "username"),
        ({"password": "short"}, "password"),
    ],
)
def test_invalid_user_fields_are_rejected(client, overrides, field):
    response = create_test_user(client, **overrides)

    assert response.status_code == 422
    assert field in {error["loc"][-1] for error in response.json()["detail"]}
