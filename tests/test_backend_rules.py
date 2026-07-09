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
from app.routers.expenses import confirm_expense, create_expense, delete_expense
from app.routers.ledgers import accept_invitation, create_ledger, get_ledger, get_ledgers, remove_member
from app.routers import users as users_router
from app.schemas.ledger import LedgerCreate, MemberCreate
from app.routers.settlements import create_settlement, get_settlements
from app.schemas.expense import ConfirmExpenseRequest, ExpenseCreate, ExpenseSplitCreate
from app.schemas.settlement import SettlementCreate
from app.services import verification
from app.services.auth import get_password_hash
from app.services.voice_expense import create_voice_expense_draft
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

    db.add(LedgerMember(ledger_id=ledger.id, user_id=owner.id, display_name=owner.display_name))
    if with_temp_member:
        db.add(LedgerMember(
            ledger_id=ledger.id,
            user_id=None,
            display_name="Temporary",
        ))
    db.commit()
    return ledger


def add_member(db, ledger, user):
    db.add(LedgerMember(ledger_id=ledger.id, user_id=user.id, display_name=user.display_name))
    db.commit()


def test_voice_expense_draft_endpoint_returns_ai_draft(db, client, monkeypatch):
    owner = make_login_user(db, "voice-owner@example.com", "Owner")
    friend = make_user(db, "voice-friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)
    memberships = db.query(LedgerMember).filter_by(ledger_id=ledger.id).all()
    owner_member = next(item for item in memberships if item.user_id == owner.id)

    def fake_create_voice_expense_draft(**kwargs):
        assert kwargs["audio"] == b"recording"
        assert {item["name"] for item in kwargs["members"]} == {"Owner", "Friend"}
        return {
            "transcript": "午饭 88 元，我付，和 Friend 一起",
            "title": "午饭",
            "amount": Decimal("88.00"),
            "payer_user_id": str(owner.id),
            "participant_member_ids": [str(owner_member.id)],
            "confirmation_text": "已生成午饭，请确认。",
        }

    monkeypatch.setattr(
        "app.routers.expenses.create_voice_expense_draft",
        fake_create_voice_expense_draft,
    )
    assert client.post(
        "/auth/login",
        data={"username": owner.email, "password": "secret123"},
    ).status_code == 200

    response = client.post(
        f"/expenses/ledgers/{ledger.id}/voice-draft",
        files={"audio": ("voice.m4a", b"recording", "audio/mp4")},
    )

    assert response.status_code == 200
    assert response.json()["amount"] == "88.00"
    assert response.json()["participant_member_ids"] == [str(owner_member.id)]


def test_voice_expense_draft_filters_unknown_and_duplicate_members(monkeypatch):
    owner_id = str(uuid.uuid4())
    owner_member_id = str(uuid.uuid4())
    friend_member_id = str(uuid.uuid4())
    unknown_member_id = str(uuid.uuid4())
    members = [
        {
            "member_id": owner_member_id,
            "user_id": owner_id,
            "name": "Owner",
            "registered": True,
        },
        {
            "member_id": friend_member_id,
            "user_id": None,
            "name": "Friend",
            "registered": False,
        },
    ]
    monkeypatch.setattr(
        "app.services.voice_expense.transcribe_audio",
        lambda *_: "午饭 88 元",
    )
    monkeypatch.setattr(
        "app.services.voice_expense.parse_expense_draft",
        lambda *_: {
            "title": "午饭",
            "amount": 88,
            "payer_user_id": "not-a-ledger-user",
            "participant_member_ids": [
                friend_member_id,
                friend_member_id,
                unknown_member_id,
            ],
        },
    )

    draft = create_voice_expense_draft(
        audio=b"recording",
        filename="voice.m4a",
        content_type="audio/mp4",
        members=members,
        current_user_id=owner_id,
    )

    assert draft["payer_user_id"] == owner_id
    assert draft["participant_member_ids"] == [friend_member_id, owner_member_id]
    assert draft["total_amount"] == Decimal("88.00")
    assert draft["split_type"] == "equal"
    assert draft["splits"] == [
        {
            "member_id": friend_member_id,
            "user_id": None,
            "amount": Decimal("44.00"),
        },
        {
            "member_id": owner_member_id,
            "user_id": owner_id,
            "amount": Decimal("44.00"),
        },
    ]


def test_voice_expense_draft_accepts_valid_exact_splits(monkeypatch):
    owner_id = str(uuid.uuid4())
    owner_member_id = str(uuid.uuid4())
    friend_member_id = str(uuid.uuid4())
    members = [
        {
            "member_id": owner_member_id,
            "user_id": owner_id,
            "name": "Owner",
            "registered": True,
        },
        {
            "member_id": friend_member_id,
            "user_id": None,
            "name": "Friend",
            "registered": False,
        },
    ]
    monkeypatch.setattr(
        "app.services.voice_expense.transcribe_audio",
        lambda *_: "午饭 88 元，我 58，Friend 30",
    )
    monkeypatch.setattr(
        "app.services.voice_expense.parse_expense_draft",
        lambda *_: {
            "title": "午饭",
            "amount": 88,
            "currency": "CNY",
            "category": "餐饮",
            "note": "午饭",
            "payer_user_id": owner_id,
            "participant_member_ids": [owner_member_id, friend_member_id],
            "split_type": "exact",
            "splits": [
                {"member_id": owner_member_id, "amount": 58},
                {"member_id": friend_member_id, "amount": 30},
            ],
            "confidence": 0.92,
            "missing_fields": [],
        },
    )

    draft = create_voice_expense_draft(
        audio=b"recording",
        filename="voice.m4a",
        content_type="audio/mp4",
        members=members,
        current_user_id=owner_id,
    )

    assert draft["category"] == "餐饮"
    assert draft["note"] == "午饭"
    assert draft["split_type"] == "exact"
    assert draft["splits"] == [
        {
            "member_id": owner_member_id,
            "user_id": owner_id,
            "amount": Decimal("58.00"),
        },
        {
            "member_id": friend_member_id,
            "user_id": None,
            "amount": Decimal("30.00"),
        },
    ]


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

    expense = create_expense(
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
            LedgerMember.user_id.is_(None),
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


def test_transfer_flow_appears_only_after_every_participant_confirms(db):
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

    assert get_settlements(ledger.id, db=db, current_user=owner) == []

    confirm_expense(expense.id, ConfirmExpenseRequest(status="confirmed"), db=db, current_user=friend)

    suggestions = get_settlements(ledger.id, db=db, current_user=owner)
    assert len(suggestions) == 1
    assert suggestions[0].from_user_id == friend.id
    assert suggestions[0].to_user_id == owner.id
    assert suggestions[0].amount == Decimal("5.00")


def test_recorded_settlement_does_not_change_generated_transfer_flow(db):
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)

    expense = create_expense(
        ledger.id,
        ExpenseCreate(
            title="Dinner",
            total_amount=Decimal("10.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[
                ExpenseSplitCreate(user_id=owner.id, amount=Decimal("5.00")),
                ExpenseSplitCreate(user_id=friend.id, amount=Decimal("5.00")),
            ],
        ),
        db=db,
        current_user=owner,
    )
    confirm_expense(expense.id, ConfirmExpenseRequest(status="confirmed"), db=db, current_user=friend)

    # Legacy transfer records may remain in the database, but no longer affect
    # the flow generated from confirmed expenses.
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
    assert suggestions[0].amount == Decimal("5.00")


def test_transfer_flow_accumulates_all_confirmed_expenses(db):
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

    # Transfer records do not clear or acknowledge the generated flow.
    assert get_settlements(ledger.id, db=db, current_user=owner)[0].amount == Decimal("50.00")

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
    assert suggestions[0].amount == Decimal("80.00")


def test_cannot_remove_member_with_unsettled_balance(db):
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


def test_can_remove_settled_member_and_preserve_history(db):
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)
    membership = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger.id,
        LedgerMember.user_id == friend.id,
    ).one()

    expense = create_expense(
        ledger.id,
        ExpenseCreate(
            title="Taxi",
            total_amount=Decimal("8.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[
                ExpenseSplitCreate(user_id=owner.id, amount=Decimal("4.00")),
                ExpenseSplitCreate(user_id=friend.id, amount=Decimal("4.00")),
            ],
        ),
        db=db,
        current_user=owner,
    )
    confirm_expense(
        expense.id,
        ConfirmExpenseRequest(status="confirmed"),
        db=db,
        current_user=friend,
    )
    create_settlement(
        ledger.id,
        SettlementCreate(
            from_user_id=friend.id,
            to_user_id=owner.id,
            amount=Decimal("4.00"),
        ),
        db=db,
        current_user=owner,
    )

    remove_member(ledger.id, friend.id, db=db, current_user=owner)

    db.refresh(membership)
    assert membership.status == "removed"
    assert db.query(Expense).filter(Expense.id == expense.id).first() is not None


def test_owner_can_delete_another_members_confirmed_expense(db):
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)

    expense = create_expense(
        ledger.id,
        ExpenseCreate(
            title="Lunch",
            total_amount=Decimal("12.00"),
            expense_date=date.today(),
            payer_id=friend.id,
            splits=[
                ExpenseSplitCreate(user_id=owner.id, amount=Decimal("6.00")),
                ExpenseSplitCreate(user_id=friend.id, amount=Decimal("6.00")),
            ],
        ),
        db=db,
        current_user=friend,
    )
    confirm_expense(
        expense.id,
        ConfirmExpenseRequest(status="confirmed"),
        db=db,
        current_user=owner,
    )

    delete_expense(expense.id, db=db, current_user=owner)

    assert db.query(Expense).filter(Expense.id == expense.id).first() is None


def test_unrelated_member_cannot_delete_expense(db):
    owner = make_user(db, "owner@example.com", "Owner")
    creator = make_user(db, "creator@example.com", "Creator")
    observer = make_user(db, "observer@example.com", "Observer")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, creator)
    add_member(db, ledger, observer)

    expense = create_expense(
        ledger.id,
        ExpenseCreate(
            title="Coffee",
            total_amount=Decimal("9.00"),
            expense_date=date.today(),
            payer_id=creator.id,
            splits=[
                ExpenseSplitCreate(user_id=creator.id, amount=Decimal("9.00")),
            ],
        ),
        db=db,
        current_user=creator,
    )

    with pytest.raises(HTTPException) as exc_info:
        delete_expense(expense.id, db=db, current_user=observer)

    assert_http_error(exc_info, 403)
    assert db.query(Expense).filter(Expense.id == expense.id).first() is not None


def test_owner_can_remove_temporary_member_by_member_id(db):
    owner = make_user(db, "owner@example.com", "Owner")
    ledger = make_ledger(db, owner, with_temp_member=True)
    temp_member = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger.id,
        LedgerMember.user_id.is_(None),
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
        LedgerMember.user_id.is_(None),
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


def test_two_user_collaboration_flow_through_api(db, client):
    owner = make_login_user(db, "cwq@example.com", "cwq")
    friend = make_login_user(db, "stella@example.com", "Stella")

    owner_client = client
    friend_client = TestClient(app)
    try:
        assert owner_client.post(
            "/auth/login",
            data={"username": owner.email, "password": "secret123"},
        ).status_code == 200
        assert friend_client.post(
            "/auth/login",
            data={"username": friend.email, "password": "secret123"},
        ).status_code == 200

        create_response = owner_client.post(
            "/ledgers",
            json={"name": "cwq and Stella", "currency": "CNY", "members": []},
        )
        assert create_response.status_code == 201
        ledger_id = create_response.json()["id"]

        invite_response = owner_client.post(
            f"/ledgers/{ledger_id}/members",
            json={
                "user_id": str(friend.id),
                "nickname": "Stella",
                "is_temporary": False,
            },
        )
        assert invite_response.status_code == 201
        assert invite_response.json()["status"] == "pending"

        invitations_response = friend_client.get("/ledgers/invitations/pending")
        assert invitations_response.status_code == 200
        invitations = invitations_response.json()
        assert len(invitations) == 1
        assert invitations[0]["invited_by_name"] == "cwq"

        invitation_id = invitations[0]["id"]
        assert friend_client.post(
            f"/ledgers/invitations/{invitation_id}/accept"
        ).status_code == 204

        for session in (owner_client, friend_client):
            ledgers_response = session.get("/ledgers")
            assert ledgers_response.status_code == 200
            assert [row["id"] for row in ledgers_response.json()] == [ledger_id]

            detail_response = session.get(f"/ledgers/{ledger_id}")
            assert detail_response.status_code == 200
            members = detail_response.json()["members"]
            assert {member["user_id"] for member in members} == {
                str(owner.id),
                str(friend.id),
            }
            assert {member["status"] for member in members} == {"active"}

        expense_response = owner_client.post(
            f"/expenses/ledgers/{ledger_id}/expenses",
            json={
                "title": "Dinner",
                "total_amount": "100.00",
                "expense_date": date.today().isoformat(),
                "payer_id": str(owner.id),
                "splits": [
                    {"user_id": str(owner.id), "amount": "50.00"},
                    {"user_id": str(friend.id), "amount": "50.00"},
                ],
            },
        )
        assert expense_response.status_code == 201
        expense_id = expense_response.json()["id"]

        pending_overview = friend_client.get(f"/ledgers/{ledger_id}/overview")
        assert pending_overview.status_code == 200
        assert pending_overview.json()["settlement_suggestions"] == []

        confirm_response = friend_client.post(
            f"/expenses/{expense_id}/confirm",
            json={"status": "confirmed"},
        )
        assert confirm_response.status_code == 200

        for session in (owner_client, friend_client):
            overview_response = session.get(f"/ledgers/{ledger_id}/overview")
            assert overview_response.status_code == 200
            overview = overview_response.json()
            assert len(overview["ledger"]["members"]) == 2
            assert overview["expenses"][0]["status"] == "confirmed"
            assert overview["settlement_suggestions"] == [{
                "from_user_id": str(friend.id),
                "from_user_name": "Stella",
                "to_user_id": str(owner.id),
                "to_user_name": "cwq",
                "amount": "50.00",
            }]

        delete_response = owner_client.delete(f"/expenses/{expense_id}")
        assert delete_response.status_code == 204
        final_overview = friend_client.get(f"/ledgers/{ledger_id}/overview").json()
        assert final_overview["expenses"] == []
        assert final_overview["settlement_suggestions"] == []
    finally:
        friend_client.close()


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
        ledger_id=ledger.id, user_id=friend.id, display_name=friend.display_name,
        status="pending",
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
        ledger_id=ledger.id, user_id=outsider.id, display_name=outsider.display_name,
        status="pending",
    ))
    db.commit()

    # Owner pays 60, split owner/friend 30/30. Outsider is pending, should NOT be part of anything.
    expense = create_expense(
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
    confirm_expense(
        expense.id,
        ConfirmExpenseRequest(status="confirmed"),
        db=db,
        current_user=friend,
    )

    suggestions = get_settlements(ledger.id, db=db, current_user=owner)
    # Only friend -> owner 30 should appear; outsider must not show up
    assert len(suggestions) == 1
    assert suggestions[0].from_user_id == friend.id
    assert suggestions[0].to_user_id == owner.id
    assert suggestions[0].amount == Decimal("30.00")
