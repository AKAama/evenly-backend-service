import uuid
from io import BytesIO
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database import get_db
from app.models import (
    AuthIdentity,
    Expense,
    ExpenseConfirmation,
    ExpenseSplit,
    ExpenseStatus,
    Ledger,
    LedgerMember,
    Settlement,
    User,
)
from app.routers.expenses import confirm_expense, create_expense
from app.routers.ledgers import accept_invitation, create_ledger, get_ledger, get_ledgers, remove_member
from app.routers import users as users_router
from app.schemas.ledger import LedgerCreate, MemberCreate
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
        username=email.split("@", 1)[0],
        display_name=display_name,
        password_hash="hashed",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_login_user(db, email, display_name, password="secret123"):
    password_hash = get_password_hash(password)
    user = User(
        id=uuid.uuid4(),
        email=email,
        username=email.split("@", 1)[0],
        display_name=display_name,
        password_hash=password_hash,
    )
    db.add(user)
    db.flush()
    db.add(AuthIdentity(
        user_id=user.id,
        provider="password",
        provider_subject=email.lower(),
        email=email.lower(),
        password_hash=password_hash,
    ))
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


def test_registered_member_must_accept_ledger_invitation(db):
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")

    response = create_ledger(
        LedgerCreate(
            name="Trip",
            members=[MemberCreate(user_id=friend.id, nickname="Friend")],
        ),
        db=db,
        current_user=owner,
    )
    invitation = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == response.id,
        LedgerMember.user_id == friend.id,
    ).one()

    assert invitation.status == "pending"
    assert get_ledgers(db=db, current_user=friend) == []
    with pytest.raises(HTTPException) as exc_info:
        get_ledger(response.id, db=db, current_user=friend)
    assert_http_error(exc_info, 403)

    accept_invitation(invitation.id, db=db, current_user=friend)
    assert len(get_ledgers(db=db, current_user=friend)) == 1


def test_ledger_summary_counts_members_and_expenses(db):
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner, with_temp_member=True)
    add_member(db, ledger, friend)

    create_expense(
        ledger.id,
        ExpenseCreate(
            title="Lunch",
            total_amount=Decimal("12.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[
                ExpenseSplitCreate(user_id=owner.id, amount=Decimal("6.00")),
                ExpenseSplitCreate(user_id=friend.id, amount=Decimal("6.00")),
            ],
        ),
        db=db,
        current_user=owner,
    )

    result = get_ledgers(db=db, current_user=owner)

    assert len(result) == 1
    assert result[0].member_count == 3
    assert result[0].expense_count == 1


@pytest.mark.asyncio
async def test_avatar_storage_failure_returns_bad_gateway(db, monkeypatch):
    user = make_user(db, "owner@example.com", "Owner")

    class FailingStorage:
        def upload_file(self, **_kwargs):
            raise RuntimeError("storage unavailable")

    monkeypatch.setattr(users_router.settings, "cos", object())
    monkeypatch.setattr(users_router, "get_cos_service", lambda: FailingStorage())
    upload = UploadFile(
        file=BytesIO(b"fake-jpeg"),
        filename="avatar.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )

    with pytest.raises(HTTPException) as exc_info:
        await users_router.upload_avatar(file=upload, current_user=user, db=db)

    assert_http_error(exc_info, 502)
    assert exc_info.value.detail == "Avatar storage is temporarily unavailable"


def test_delete_account_removes_owned_and_shared_user_data(db):
    owner = make_user(db, "owner@example.com", "Owner")
    deleting_user = make_user(db, "delete@example.com", "Delete Me")
    owned_ledger = make_ledger(db, deleting_user)
    shared_ledger = make_ledger(db, owner)
    add_member(db, shared_ledger, deleting_user)

    expense = Expense(
        ledger_id=shared_ledger.id,
        payer_id=deleting_user.id,
        created_by=deleting_user.id,
        title="Shared lunch",
        total_amount=Decimal("20.00"),
        expense_date=date.today(),
        status=ExpenseStatus.PENDING,
    )
    db.add(expense)
    db.flush()
    membership = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == shared_ledger.id,
        LedgerMember.user_id == deleting_user.id,
    ).one()
    db.add(ExpenseSplit(
        expense_id=expense.id,
        user_id=deleting_user.id,
        member_id=membership.id,
        amount=Decimal("20.00"),
    ))
    db.add(ExpenseConfirmation(
        expense_id=expense.id,
        user_id=deleting_user.id,
        status="confirmed",
    ))
    db.add(Settlement(
        ledger_id=shared_ledger.id,
        from_user_id=deleting_user.id,
        to_user_id=owner.id,
        amount=Decimal("5.00"),
    ))
    db.commit()

    users_router.delete_account(current_user=deleting_user, db=db)

    assert db.query(User).filter(User.id == deleting_user.id).first() is None
    assert db.query(Ledger).filter(Ledger.id == owned_ledger.id).first() is None
    assert db.query(Ledger).filter(Ledger.id == shared_ledger.id).first() is not None
    assert db.query(LedgerMember).filter(LedgerMember.user_id == deleting_user.id).count() == 0
    assert db.query(Expense).filter(Expense.id == expense.id).first() is None
    assert db.query(Settlement).filter(
        (Settlement.from_user_id == deleting_user.id)
        | (Settlement.to_user_id == deleting_user.id)
    ).count() == 0


def test_delete_account_endpoint_requires_authentication(client):
    response = client.delete("/users/me")

    assert response.status_code == 401


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


def test_expense_split_schema_accepts_ledger_member_id():
    assert "member_id" in ExpenseSplitCreate.model_fields


def test_create_expense_allows_temporary_member_split(db):
    owner = make_user(db, "owner@example.com", "Owner")
    ledger = make_ledger(db, owner, with_temp_member=True)
    owner_member = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger.id,
        LedgerMember.user_id == owner.id,
    ).one()
    temporary_member = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger.id,
        LedgerMember.is_temporary.is_(True),
    ).one()

    created = create_expense(
        ledger.id,
        ExpenseCreate(
            title="Lunch",
            total_amount=Decimal("12.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[
                ExpenseSplitCreate(member_id=owner_member.id, amount=Decimal("6.00")),
                ExpenseSplitCreate(member_id=temporary_member.id, amount=Decimal("6.00")),
            ],
        ),
        db=db,
        current_user=owner,
    )

    splits = db.query(ExpenseSplit).filter(ExpenseSplit.expense_id == created.id).all()
    assert {split.member_id for split in splits} == {owner_member.id, temporary_member.id}
    assert next(split for split in splits if split.member_id == temporary_member.id).user_id is None


def test_create_expense_resolves_member_id_from_registered_user_id(db):
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)
    members = db.query(LedgerMember).filter(LedgerMember.ledger_id == ledger.id).all()
    member_by_user = {member.user_id: member for member in members}

    created = create_expense(
        ledger.id,
        ExpenseCreate(
            title="Dinner",
            total_amount=Decimal("500.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[
                ExpenseSplitCreate(user_id=owner.id, amount=Decimal("250.00")),
                ExpenseSplitCreate(user_id=friend.id, amount=Decimal("250.00")),
            ],
        ),
        db=db,
        current_user=owner,
    )

    splits = db.query(ExpenseSplit).filter(ExpenseSplit.expense_id == created.id).all()
    assert {split.member_id for split in splits} == {
        member_by_user[owner.id].id,
        member_by_user[friend.id].id,
    }


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

    creator_confirmation = db.query(ExpenseConfirmation).filter(
        ExpenseConfirmation.expense_id == expense.id,
        ExpenseConfirmation.user_id == owner.id,
    ).one()
    assert creator_confirmation.status == "confirmed"
    assert expense.status == ExpenseStatus.CONFIRMED

    with pytest.raises(HTTPException) as exc_info:
        confirm_expense(expense.id, ConfirmExpenseRequest(status="confirmed"), db=db, current_user=observer)
    assert_http_error(exc_info, 403)


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


def test_recorded_settlement_reduces_remaining_settlement_amount(db):
    """Recording a partial settlement reduces the remaining suggested amount."""
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
    confirm_expense(expense.id, ConfirmExpenseRequest(status="confirmed"), db=db, current_user=friend)

    suggestions = get_settlements(ledger.id, db=db, current_user=owner)
    assert len(suggestions) == 1
    assert suggestions[0].from_user_id == friend.id
    assert suggestions[0].to_user_id == owner.id
    assert suggestions[0].amount == Decimal("5.00")

    # Partial payment of 2 -> remaining should be 3
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
    assert len(suggestions) == 1
    assert suggestions[0].from_user_id == friend.id
    assert suggestions[0].to_user_id == owner.id
    assert suggestions[0].amount == Decimal("3.00")


def test_full_settlement_clears_suggestions_and_new_expense_only_counts_new_debt(db):
    """After a bill is fully settled, a new expense must not double-count the old one.

    Scenario (the reported bug):
      1. A pays 100 for dinner, B owes A 50.
      2. B pays A 50 via settlement; the ledger is balanced.
      3. A pays another 60 for lunch, B owes A 30.
      4. The suggestion should be B->A 30, NOT B->A 80.
    """
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)

    # First expense: owner paid 100, split 50/50
    dinner = create_expense(
        ledger.id,
        ExpenseCreate(
            title="Dinner",
            total_amount=Decimal("100.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[
                ExpenseSplitCreate(user_id=owner.id, amount=Decimal("50.00")),
                ExpenseSplitCreate(user_id=friend.id, amount=Decimal("50.00")),
            ],
        ),
        db=db,
        current_user=owner,
    )
    confirm_expense(dinner.id, ConfirmExpenseRequest(status="confirmed"), db=db, current_user=friend)

    assert get_settlements(ledger.id, db=db, current_user=owner)[0].amount == Decimal("50.00")

    # Friend pays in full
    create_settlement(
        ledger.id,
        SettlementCreate(from_user_id=friend.id, to_user_id=owner.id, amount=Decimal("50.00")),
        db=db,
        current_user=owner,
    )

    # Everything should be settled now
    assert get_settlements(ledger.id, db=db, current_user=owner) == []

    # Second expense: owner pays 60, split 30/30
    lunch = create_expense(
        ledger.id,
        ExpenseCreate(
            title="Lunch",
            total_amount=Decimal("60.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[
                ExpenseSplitCreate(user_id=owner.id, amount=Decimal("30.00")),
                ExpenseSplitCreate(user_id=friend.id, amount=Decimal("30.00")),
            ],
        ),
        db=db,
        current_user=owner,
    )
    confirm_expense(lunch.id, ConfirmExpenseRequest(status="confirmed"), db=db, current_user=friend)

    suggestions = get_settlements(ledger.id, db=db, current_user=owner)
    assert len(suggestions) == 1
    assert suggestions[0].from_user_id == friend.id
    assert suggestions[0].to_user_id == owner.id
    # Bug would have produced 50 + 30 = 80; correct answer is 30
    assert suggestions[0].amount == Decimal("30.00")


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


def test_member_cannot_remove_self_through_owner_endpoint(db):
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)

    with pytest.raises(HTTPException) as exc_info:
        remove_member(ledger.id, friend.id, db=db, current_user=friend)

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


def test_verification_codes_are_isolated_by_purpose(monkeypatch):
    verification.verification_codes.clear()
    monkeypatch.setattr(verification.settings, "redis_url", None)
    monkeypatch.setattr(verification, "_redis_client", None)
    monkeypatch.setattr(verification, "generate_code", lambda length=6: "112233")
    monkeypatch.setattr("app.services.email.get_email_service", lambda: None)

    assert verification.send_verification_code("user@example.com", purpose="email_change") is True
    assert verification.verify_code("user@example.com", "112233", purpose="password_reset") is False
    assert verification.verify_code("user@example.com", "112233", purpose="email_change") is True


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


def test_responses_include_server_timing_headers(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert float(response.headers["X-Process-Time-Ms"]) >= 0
    assert response.headers["Server-Timing"].startswith("app;dur=")


def test_ledger_overview_returns_main_screen_payload(db, client):
    user = make_login_user(db, "owner@example.com", "Owner")
    ledger = make_ledger(db, user)
    login_response = client.post(
        "/auth/login",
        data={"username": "owner@example.com", "password": "secret123"},
    )
    assert login_response.status_code == 200

    response = client.get(f"/ledgers/{ledger.id}/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ledger"]["id"] == str(ledger.id)
    assert len(payload["ledger"]["members"]) == 1
    assert payload["expenses"] == []
    assert payload["settlement_suggestions"] == []
    assert payload["settlement_history"] == []


def test_login_accepts_case_insensitive_username(db, client):
    make_login_user(db, "owner@example.com", "Owner")

    response = client.post(
        "/auth/login",
        data={"username": "OWNER", "password": "secret123"},
    )

    assert response.status_code == 200


def test_password_login_uses_identity_credentials(db, client):
    user = make_login_user(db, "owner@example.com", "Owner")
    identity = db.query(AuthIdentity).filter_by(user_id=user.id, provider="password").one()
    identity.password_hash = get_password_hash("identity-password")
    db.commit()

    old_response = client.post(
        "/auth/login",
        data={"username": "owner@example.com", "password": "secret123"},
    )
    new_response = client.post(
        "/auth/login",
        data={"username": "owner@example.com", "password": "identity-password"},
    )

    assert old_response.status_code == 401
    assert new_response.status_code == 200


def test_apple_login_creates_identity_and_reuses_account(db, client, monkeypatch):
    claims = {
        "sub": "apple-user-123",
        "email": "apple@example.com",
        "email_verified": True,
    }
    monkeypatch.setattr(
        "app.routers.auth.verify_apple_identity_token",
        lambda identity_token, nonce: claims,
    )

    first = client.post(
        "/auth/apple",
        json={"identity_token": "token", "nonce": "nonce", "full_name": "Apple User"},
    )
    second = client.post(
        "/auth/apple",
        json={"identity_token": "token", "nonce": "nonce"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert db.query(User).filter(User.email == "apple@example.com").count() == 1
    identity = db.query(AuthIdentity).filter_by(
        provider="apple", provider_subject="apple-user-123"
    ).one()
    assert identity.user.display_name == "Apple User"
    assert identity.user.username_is_generated is True


def test_apple_user_can_choose_username_and_set_password(db, client, monkeypatch):
    verification.verification_codes.clear()
    monkeypatch.setattr(verification.settings, "redis_url", None)
    monkeypatch.setattr(verification, "_redis_client", None)
    monkeypatch.setattr(verification, "generate_code", lambda length=6: "246810")
    monkeypatch.setattr("app.services.email.get_email_service", lambda: None)
    monkeypatch.setattr(
        "app.routers.auth.verify_apple_identity_token",
        lambda identity_token, nonce: {
            "sub": "apple-setup-user",
            "email": "setup@example.com",
            "email_verified": True,
        },
    )
    assert client.post(
        "/auth/apple",
        json={"identity_token": "token", "nonce": "nonce"},
    ).status_code == 200

    methods = client.get("/users/me/auth-methods").json()
    assert methods == {"methods": ["apple"], "has_password": False}

    username_response = client.put(
        "/users/me/username", json={"username": "apple_friend"}
    )
    assert username_response.status_code == 200
    assert username_response.json()["username_is_generated"] is False

    assert client.post("/users/me/password/setup/send").status_code == 200
    setup_response = client.put(
        "/users/me/password/setup",
        json={"code": "246810", "new_password": "new-secret"},
    )
    assert setup_response.status_code == 200
    assert client.get("/users/me/auth-methods").json() == {
        "methods": ["apple", "password"],
        "has_password": True,
    }
    assert client.post(
        "/auth/login",
        data={"username": "apple_friend", "password": "new-secret"},
    ).status_code == 200


def test_get_ledger_returns_pending_members_with_status(db):
    """GET /ledgers/{id} should include pending members with status='pending' so owners can see outstanding invites."""
    from app.routers.ledgers import get_ledger

    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    # Simulate an invited-but-not-yet-accepted member
    db.add(LedgerMember(
        ledger_id=ledger.id, user_id=friend.id, nickname=friend.display_name,
        status="pending", invited_by=owner.id,
    ))
    db.commit()

    detail = get_ledger(ledger.id, db=db, current_user=owner)
    statuses = {m.user_id: m.status for m in detail.members}
    assert statuses[owner.id] == "active"
    assert statuses[friend.id] == "pending"

    # The pending friend themselves should NOT be able to access the ledger
    # (require_ledger_member enforces active)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        get_ledger(ledger.id, db=db, current_user=friend)
    assert exc_info.value.status_code == 403


def test_settlement_excludes_pending_members(db):
    """Pending invitations must not participate in settlement calculations."""
    from app.routers.expenses import create_expense
    from app.routers.settlements import get_settlements
    from app.schemas.expense import ExpenseCreate, ExpenseSplitCreate
    from datetime import date

    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    outsider = make_user(db, "outsider@example.com", "Outsider")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)  # active (via helper which inserts active)
    # outsider is invited but hasn't accepted
    db.add(LedgerMember(
        ledger_id=ledger.id, user_id=outsider.id, nickname=outsider.display_name,
        status="pending", invited_by=owner.id,
    ))
    db.commit()

    # Owner pays 60, split owner/friend 30/30. Outsider is pending, should NOT be part of anything.
    create_expense(
        ledger.id,
        ExpenseCreate(
            title="Lunch",
            total_amount=Decimal("60.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[
                ExpenseSplitCreate(user_id=owner.id, amount=Decimal("30.00")),
                ExpenseSplitCreate(user_id=friend.id, amount=Decimal("30.00")),
            ],
        ),
        db=db,
        current_user=owner,
    )

    suggestions = get_settlements(ledger.id, db=db, current_user=owner)
    # Only friend -> owner 30 should appear; outsider must not show up
    assert len(suggestions) == 1
    assert suggestions[0].from_user_id == friend.id
    assert suggestions[0].to_user_id == owner.id
    assert suggestions[0].amount == Decimal("30.00")
