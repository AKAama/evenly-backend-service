import logging
import time
from enum import Enum
from typing import Iterable
from uuid import UUID

import httpx
from jose import jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PushDevice


logger = logging.getLogger(__name__)


class PushEvent(str, Enum):
    EXPENSE_CREATED = "expense.created"
    LEDGER_INVITED = "ledger.invited"
    EXPENSE_CONFIRMED = "expense.confirmed"
    EXPENSE_REJECTED = "expense.rejected"


TITLES = {
    PushEvent.EXPENSE_CREATED: "有一笔新账单待确认",
    PushEvent.LEDGER_INVITED: "你收到一个账本邀请",
    PushEvent.EXPENSE_CONFIRMED: "账单已确认",
    PushEvent.EXPENSE_REJECTED: "账单被否决",
}


def build_payload(
    *,
    event: PushEvent,
    actor_name: str,
    ledger_name: str,
    ledger_id: str,
    expense_name: str | None = None,
    expense_id: str | None = None,
) -> dict:
    actor = actor_name[:40]
    ledger = ledger_name[:60]
    expense = (expense_name or "账单")[:80]
    if event == PushEvent.EXPENSE_CREATED:
        body = f"{actor} 在「{ledger}」添加了“{expense}”"
    elif event == PushEvent.LEDGER_INVITED:
        body = f"{actor} 邀请你加入「{ledger}」"
    elif event == PushEvent.EXPENSE_CONFIRMED:
        body = f"{actor} 已确认“{expense}”"
    else:
        body = f"{actor} 已否决“{expense}”"
    payload = {
        "aps": {"alert": {"title": TITLES[event], "body": body}, "sound": "default"},
        "event": event.value,
        "ledger_id": ledger_id,
    }
    if expense_id:
        payload["expense_id"] = expense_id
    return payload


def _provider_token() -> str | None:
    if not settings.apns_team_id or not settings.apns_key_id or not settings.apns_private_key:
        return None
    private_key = settings.apns_private_key.replace("\\n", "\n")
    return jwt.encode(
        {"iss": settings.apns_team_id, "iat": int(time.time())},
        private_key,
        algorithm="ES256",
        headers={"kid": settings.apns_key_id},
    )


def send_push_to_users(db: Session, user_ids: Iterable[UUID], payload: dict) -> None:
    auth_token = _provider_token()
    if auth_token is None:
        logger.info("APNs not configured; skipping event=%s", payload.get("event"))
        return
    ids = set(user_ids)
    if not ids:
        return
    devices = db.query(PushDevice).filter(
        PushDevice.user_id.in_(ids),
        PushDevice.is_active.is_(True),
    ).all()
    with httpx.Client(http2=True, timeout=10) as client:
        for device in devices:
            host = "api.sandbox.push.apple.com" if device.environment == "sandbox" else "api.push.apple.com"
            response = client.post(
                f"https://{host}/3/device/{device.token}",
                json=payload,
                headers={
                    "authorization": f"bearer {auth_token}",
                    "apns-topic": device.bundle_id,
                    "apns-push-type": "alert",
                    "apns-priority": "10",
                },
            )
            if response.status_code == 200:
                continue
            reason = response.json().get("reason", "unknown") if response.content else "unknown"
            if reason in {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"}:
                device.is_active = False
            logger.warning("APNs delivery failed event=%s status=%d reason=%s", payload.get("event"), response.status_code, reason)
    db.commit()


def send_push_safely(db: Session, user_ids: Iterable[UUID], payload: dict) -> None:
    try:
        send_push_to_users(db, user_ids, payload)
    except Exception:
        logger.exception("APNs dispatch failed event=%s", payload.get("event"))
