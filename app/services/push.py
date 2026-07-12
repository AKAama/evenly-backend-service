import logging
import time
from enum import Enum
from typing import Iterable
from uuid import UUID

import httpx
from jose import jwt
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PushDevice
from app.services import redis_client


logger = logging.getLogger(__name__)

# Apple allows provider tokens for up to ~1 hour; refresh slightly earlier.
_APNS_TOKEN_CACHE_KEY = "evenly:apns:provider_jwt"
_APNS_TOKEN_TTL_SECONDS = 50 * 60


class PushEvent(str, Enum):
    EXPENSE_CREATED = "expense.created"
    EXPENSE_UPDATED = "expense.updated"
    LEDGER_INVITED = "ledger.invited"
    EXPENSE_CONFIRMED = "expense.confirmed"
    EXPENSE_REJECTED = "expense.rejected"


TITLES = {
    PushEvent.EXPENSE_CREATED: "有一笔新账单待确认",
    PushEvent.EXPENSE_UPDATED: "账单已更新，请重新确认",
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
        body = f"{actor} 在「{ledger}」记了一笔“{expense}”"
    elif event == PushEvent.EXPENSE_UPDATED:
        body = f"{actor} 修改了「{ledger}」的“{expense}”，请重新确认"
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


def _mint_provider_token() -> str | None:
    if not settings.apns_team_id or not settings.apns_key_id or not settings.apns_private_key:
        return None
    private_key = settings.apns_private_key.replace("\\n", "\n")
    return jwt.encode(
        {"iss": settings.apns_team_id, "iat": int(time.time())},
        private_key,
        algorithm="ES256",
        headers={"kid": settings.apns_key_id},
    )


def _provider_token() -> str | None:
    """Return a cached APNs provider JWT when possible (shared across workers via Redis)."""
    client = redis_client.get_redis()
    if client is not None:
        try:
            cached = client.get(_APNS_TOKEN_CACHE_KEY)
            if cached:
                return cached
        except RedisError:
            logger.exception("Failed to read cached APNs provider token")

    token = _mint_provider_token()
    if token is None:
        return None

    if client is not None:
        try:
            client.set(_APNS_TOKEN_CACHE_KEY, token, ex=_APNS_TOKEN_TTL_SECONDS)
            logger.info("Cached APNs provider token ttl=%ss", _APNS_TOKEN_TTL_SECONDS)
        except RedisError:
            logger.exception("Failed to cache APNs provider token")
    return token


def send_push_to_users(db: Session, user_ids: Iterable[UUID], payload: dict) -> None:
    auth_token = _provider_token()
    if auth_token is None:
        logger.warning(
            "APNs not configured (set APNS_TEAM_ID / APNS_KEY_ID / APNS_PRIVATE_KEY); "
            "skipping event=%s user_count=%d",
            payload.get("event"),
            len(set(user_ids)),
        )
        return
    ids = set(user_ids)
    if not ids:
        return
    devices = db.query(PushDevice).filter(
        PushDevice.user_id.in_(ids),
        PushDevice.is_active.is_(True),
    ).all()
    if not devices:
        logger.info(
            "No active push devices for event=%s user_count=%d",
            payload.get("event"),
            len(ids),
        )
        return
    logger.info(
        "Dispatching APNs event=%s devices=%d users=%d",
        payload.get("event"),
        len(devices),
        len(ids),
    )
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
                logger.info(
                    "APNs delivered event=%s env=%s token=%s…",
                    payload.get("event"),
                    device.environment,
                    device.token[:12],
                )
                continue
            reason = response.json().get("reason", "unknown") if response.content else "unknown"
            if reason in {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"}:
                device.is_active = False
            logger.warning(
                "APNs delivery failed event=%s status=%d reason=%s env=%s token=%s…",
                payload.get("event"),
                response.status_code,
                reason,
                device.environment,
                device.token[:12],
            )
    db.commit()


def send_push_safely(db: Session, user_ids: Iterable[UUID], payload: dict) -> None:
    try:
        send_push_to_users(db, user_ids, payload)
    except Exception:
        logger.exception("APNs dispatch failed event=%s", payload.get("event"))
