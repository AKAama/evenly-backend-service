import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database import get_db
from app.models import ExpenseStatus, Ledger, LedgerMember, User
from app.routers.expenses import confirm_expense, create_expense
from app.routers.ledgers import create_ledger, get_ledger, remove_member
from app.schemas.ledger import LedgerCreate
from app.routers.settlements import create_settlement, get_settlements
from app.schemas.expense import ConfirmExpenseRequest, ExpenseCreate, ExpenseSplitCreate
from app.schemas.settlement import SettlementCreate
from app.services import verification
from app.services.auth import get_password_hash
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


def make_user(db, email, display_name):
    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name=display_name,
        password_hash="hashed",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_login_user(db, email, display_name, password="secret123"):
    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name=display_name,
        password_hash=get_password_hash(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture()
def client(db):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def make_ledger(db, owner, *, with_temp_member=False):
    ledger = Ledger(id=uuid.uuid4(), name=f"{owner.display_name}'s ledger", owner_id=owner.id, currency="CNY")
    db.add(ledger)
    db.commit()
    db.refresh(ledger)

    db.add(LedgerMember(ledger_id=ledger.id, user_id=owner.id, nickname=owner.display_name))
    if with_temp_member:
        db.add(LedgerMember(
            ledger_id=ledger.id,
            user_id=None,
            nickname="Temporary",
            is_temporary=True,
            temporary_name="Temporary",
        ))
    db.commit()
    return ledger


def add_member(db, ledger, user):
    db.add(LedgerMember(ledger_id=ledger.id, user_id=user.id, nickname=user.display_name))
    db.commit()


def assert_http_error(exc_info, status_code):
    assert exc_info.value.status_code == status_code


def test_temporary_member_does_not_authorize_unrelated_user(db):
    owner = make_user(db, "owner@example.com", "Owner")
    intruder = make_user(db, "intruder@example.com", "Intruder")
    ledger = make_ledger(db, owner, with_temp_member=True)

    with pytest.raises(HTTPException) as exc_info:
        get_ledger(ledger.id, db=db, current_user=intruder)

    assert_http_error(exc_info, 403)


def test_create_ledger_response_includes_owner_member_id(db):
    owner = make_user(db, "owner@example.com", "Owner")

    response = create_ledger(
        LedgerCreate(name="Trip", currency="CNY"),
        db=db,
        current_user=owner,
    )

    assert len(response.members) == 1
    assert response.members[0].id is not None
    assert response.members[0].user_id == owner.id


def test_create_expense_rejects_split_for_non_member(db):
    owner = make_user(db, "owner@example.com", "Owner")
    intruder = make_user(db, "intruder@example.com", "Intruder")
    ledger = make_ledger(db, owner)

    payload = ExpenseCreate(
        title="Lunch",
        total_amount=Decimal("10.00"),
        expense_date=date.today(),
        payer_id=owner.id,
        splits=[
            ExpenseSplitCreate(user_id=owner.id, amount=Decimal("5.00")),
            ExpenseSplitCreate(user_id=intruder.id, amount=Decimal("5.00")),
        ],
    )

    with pytest.raises(HTTPException) as exc_info:
        create_expense(ledger.id, payload, db=db, current_user=owner)

    assert_http_error(exc_info, 400)


def test_create_expense_rejects_non_positive_split_amount(db):
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)

    payload = ExpenseCreate(
        title="Snacks",
        total_amount=Decimal("10.00"),
        expense_date=date.today(),
        payer_id=owner.id,
        splits=[
            ExpenseSplitCreate(user_id=owner.id, amount=Decimal("11.00")),
            ExpenseSplitCreate(user_id=friend.id, amount=Decimal("-1.00")),
        ],
    )

    with pytest.raises(HTTPException) as exc_info:
        create_expense(ledger.id, payload, db=db, current_user=owner)

    assert_http_error(exc_info, 400)


def test_only_expense_participants_confirm_and_temp_members_do_not_block(db):
    owner = make_user(db, "owner@example.com", "Owner")
    observer = make_user(db, "observer@example.com", "Observer")
    ledger = make_ledger(db, owner, with_temp_member=True)
    add_member(db, ledger, observer)

    payload = ExpenseCreate(
        title="Coffee",
        total_amount=Decimal("8.00"),
        expense_date=date.today(),
        payer_id=owner.id,
        splits=[
            ExpenseSplitCreate(user_id=owner.id, amount=Decimal("8.00")),
        ],
    )
    expense = create_expense(ledger.id, payload, db=db, current_user=owner)

    with pytest.raises(HTTPException) as exc_info:
        confirm_expense(expense.id, ConfirmExpenseRequest(status="confirmed"), db=db, current_user=observer)
    assert_http_error(exc_info, 403)

    confirmed = confirm_expense(expense.id, ConfirmExpenseRequest(status="confirmed"), db=db, current_user=owner)
    assert confirmed.status == ExpenseStatus.CONFIRMED


def test_settlement_rejects_same_user_and_non_positive_amount(db):
    owner = make_user(db, "owner@example.com", "Owner")
    ledger = make_ledger(db, owner)

    same_user_payload = SettlementCreate(
        from_user_id=owner.id,
        to_user_id=owner.id,
        amount=Decimal("1.00"),
    )
    with pytest.raises(HTTPException) as exc_info:
        create_settlement(ledger.id, same_user_payload, db=db, current_user=owner)
    assert_http_error(exc_info, 400)

    zero_amount_payload = SettlementCreate(
        from_user_id=owner.id,
        to_user_id=uuid.uuid4(),
        amount=Decimal("0.00"),
    )
    with pytest.raises(HTTPException) as exc_info:
        create_settlement(ledger.id, zero_amount_payload, db=db, current_user=owner)
    assert_http_error(exc_info, 400)


def test_recorded_settlement_reduces_future_settlement_suggestions(db):
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)

    payload = ExpenseCreate(
        title="Dinner",
        total_amount=Decimal("10.00"),
        expense_date=date.today(),
        payer_id=owner.id,
        splits=[
            ExpenseSplitCreate(user_id=owner.id, amount=Decimal("5.00")),
            ExpenseSplitCreate(user_id=friend.id, amount=Decimal("5.00")),
        ],
    )
    expense = create_expense(ledger.id, payload, db=db, current_user=owner)
    confirm_expense(expense.id, ConfirmExpenseRequest(status="confirmed"), db=db, current_user=owner)
    confirm_expense(expense.id, ConfirmExpenseRequest(status="confirmed"), db=db, current_user=friend)

    suggestions = get_settlements(ledger.id, db=db, current_user=owner)
    assert suggestions[0].from_user_id == friend.id
    assert suggestions[0].to_user_id == owner.id
    assert suggestions[0].amount == Decimal("5.00")

    create_settlement(
        ledger.id,
        SettlementCreate(
            from_user_id=friend.id,
            to_user_id=owner.id,
            amount=Decimal("2.00"),
        ),
        db=db,
        current_user=owner,
    )

    suggestions = get_settlements(ledger.id, db=db, current_user=owner)
    assert suggestions[0].amount == Decimal("3.00")


def test_cannot_remove_member_with_expense_history(db):
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)

    payload = ExpenseCreate(
        title="Taxi",
        total_amount=Decimal("8.00"),
        expense_date=date.today(),
        payer_id=owner.id,
        splits=[
            ExpenseSplitCreate(user_id=owner.id, amount=Decimal("4.00")),
            ExpenseSplitCreate(user_id=friend.id, amount=Decimal("4.00")),
        ],
    )
    create_expense(ledger.id, payload, db=db, current_user=owner)

    with pytest.raises(HTTPException) as exc_info:
        remove_member(ledger.id, friend.id, db=db, current_user=owner)

    assert_http_error(exc_info, 400)


def test_owner_can_remove_temporary_member_by_member_id(db):
    owner = make_user(db, "owner@example.com", "Owner")
    ledger = make_ledger(db, owner, with_temp_member=True)
    temp_member = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger.id,
        LedgerMember.is_temporary == True,
    ).first()

    remove_member(ledger.id, temp_member.id, db=db, current_user=owner)

    assert db.query(LedgerMember).filter(LedgerMember.id == temp_member.id).first() is None


def test_non_owner_cannot_remove_temporary_member_by_member_id(db):
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner, with_temp_member=True)
    add_member(db, ledger, friend)
    temp_member = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger.id,
        LedgerMember.is_temporary == True,
    ).first()

    with pytest.raises(HTTPException) as exc_info:
        remove_member(ledger.id, temp_member.id, db=db, current_user=friend)

    assert_http_error(exc_info, 403)


def test_verification_code_is_rate_limited_and_consumed(monkeypatch):
    verification.verification_codes.clear()
    monkeypatch.setattr(verification.settings, "redis_url", None)
    monkeypatch.setattr(verification.settings, "verification_send_interval_seconds", 60)
    monkeypatch.setattr(verification, "_redis_client", None)
    monkeypatch.setattr(verification, "generate_code", lambda length=6: "123456")
    monkeypatch.setattr("app.services.email.get_email_service", lambda: None)

    assert verification.send_verification_code("USER@example.com") is True
    assert verification.send_verification_code("user@example.com") is False
    assert verification.verify_code("user@example.com", "123456") is True
    assert verification.verify_code("user@example.com", "123456") is False


def test_expired_verification_code_is_rejected(monkeypatch):
    verification.verification_codes.clear()
    monkeypatch.setattr(verification.settings, "redis_url", None)
    monkeypatch.setattr(verification.settings, "verification_code_expire_seconds", -1)
    monkeypatch.setattr(verification, "_redis_client", None)
    monkeypatch.setattr(verification, "generate_code", lambda length=6: "654321")
    monkeypatch.setattr("app.services.email.get_email_service", lambda: None)

    assert verification.send_verification_code("user@example.com") is True
    assert verification.verify_code("user@example.com", "654321") is False


def test_web_login_sets_http_only_cookie_and_logout_clears_it(db, client):
    make_login_user(db, "owner@example.com", "Owner")

    login_response = client.post(
        "/auth/login",
        data={"username": "owner@example.com", "password": "secret123"},
    )

    assert login_response.status_code == 200
    assert "evenly_access_token=" in login_response.headers["set-cookie"]
    assert "HttpOnly" in login_response.headers["set-cookie"]

    me_response = client.get("/users/me")
    assert me_response.status_code == 200
    assert me_response.json()["email"] == "owner@example.com"

    logout_response = client.post("/auth/logout")
    assert logout_response.status_code == 200
    assert "evenly_access_token=" in logout_response.headers["set-cookie"]
    assert "Max-Age=0" in logout_response.headers["set-cookie"]
