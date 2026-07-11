import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import PushDevice, User
from app.routers.users import delete_push_device, register_push_device
from app.schemas.user import PushDeviceRegistration
from app.services.push import PushEvent, build_payload


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def make_user(db, email):
    user = User(
        id=uuid.uuid4(),
        email=email,
        username=email.split("@", 1)[0],
        display_name=email,
        password_hash="hashed",
    )
    db.add(user)
    db.commit()
    return user


def test_register_refresh_transfer_and_disable_push_device(db):
    first = make_user(db, "first@example.com")
    second = make_user(db, "second@example.com")
    token = "ab" * 32
    request = PushDeviceRegistration(environment="sandbox", bundle_id="com.yhma.Evenly")

    register_push_device(token, request, first, db)
    register_push_device(token, request, second, db)

    device = db.query(PushDevice).filter_by(token=token).one()
    assert device.user_id == second.id
    assert device.is_active is True

    delete_push_device(token, second, db)
    assert device.is_active is False


def test_register_rejects_invalid_push_token(db):
    user = make_user(db, "invalid@example.com")
    request = PushDeviceRegistration(environment="production", bundle_id="com.yhma.Evenly")

    with pytest.raises(HTTPException) as error:
        register_push_device("not-a-token", request, user, db)

    assert error.value.status_code == 422


@pytest.mark.parametrize(
    ("event", "expected_title"),
    [
        (PushEvent.EXPENSE_CREATED, "有一笔新账单待确认"),
        (PushEvent.LEDGER_INVITED, "你收到一个账本邀请"),
        (PushEvent.EXPENSE_CONFIRMED, "账单已确认"),
        (PushEvent.EXPENSE_REJECTED, "账单被否决"),
    ],
)
def test_build_payload_contains_routing_contract(event, expected_title):
    payload = build_payload(
        event=event,
        actor_name="小雨",
        ledger_name="周末旅行",
        ledger_id="ledger-id",
        expense_name="晚餐",
        expense_id="expense-id" if event != PushEvent.LEDGER_INVITED else None,
    )

    assert payload["event"] == event.value
    assert payload["ledger_id"] == "ledger-id"
    assert payload["aps"]["alert"]["title"] == expected_title
    assert "amount" not in payload
    if event == PushEvent.LEDGER_INVITED:
        assert "expense_id" not in payload
    else:
        assert payload["expense_id"] == "expense-id"
