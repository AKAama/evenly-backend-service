"""Short-lived cache for pending ledger invitations (iOS polls every few seconds)."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from redis.exceptions import RedisError

from app.services import redis_client

logger = logging.getLogger(__name__)

# Slightly longer than the iOS poll interval so repeated polls hit Redis.
PENDING_INVITES_TTL_SECONDS = 45


def _key(user_id: UUID | str) -> str:
    return f"evenly:invites:pending:{user_id}"


def get_pending_invitations(user_id: UUID | str) -> list[dict[str, Any]] | None:
    """Return cached invite list, or None on miss / redis unavailable."""
    client = redis_client.get_redis()
    if client is None:
        return None
    try:
        raw = client.get(_key(user_id))
        if raw is None:
            return None
        data = json.loads(raw)
        if not isinstance(data, list):
            return None
        return data
    except (RedisError, json.JSONDecodeError, TypeError):
        logger.exception("读取待处理邀请缓存失败 user_id=%s", user_id)
        return None


def set_pending_invitations(user_id: UUID | str, payloads: list[dict[str, Any]]) -> None:
    client = redis_client.get_redis()
    if client is None:
        return
    try:
        client.set(
            _key(user_id),
            json.dumps(payloads, default=str),
            ex=PENDING_INVITES_TTL_SECONDS,
        )
    except RedisError:
        logger.exception("写入待处理邀请缓存失败 user_id=%s", user_id)


def invalidate_pending_invitations(user_id: UUID | str | None) -> None:
    if user_id is None:
        return
    client = redis_client.get_redis()
    if client is None:
        return
    try:
        client.delete(_key(user_id))
        logger.info("已失效待处理邀请缓存 user_id=%s", user_id)
    except RedisError:
        logger.exception("失效待处理邀请缓存失败 user_id=%s", user_id)


def invalidate_pending_invitations_many(user_ids: list[UUID | str | None]) -> None:
    for user_id in {uid for uid in user_ids if uid is not None}:
        invalidate_pending_invitations(user_id)
