"""Shared Redis client for verification, rate limits, caches, and APNs token storage."""

from __future__ import annotations

import logging
from typing import Optional

from redis import Redis
from redis.exceptions import RedisError

from app.config import settings

logger = logging.getLogger(__name__)

_redis_client: Redis | None = None
_redis_init_attempted = False


def get_redis() -> Redis | None:
    """Return a shared Redis client, or None when not configured / unavailable."""
    global _redis_client, _redis_init_attempted

    if not settings.redis_url:
        return None

    if _redis_client is not None:
        return _redis_client

    if _redis_init_attempted and _redis_client is None:
        # Previous init failed; allow retry on next call after reset_redis_client().
        pass

    _redis_init_attempted = True
    try:
        client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        # Fail fast if the instance is unreachable so callers can degrade cleanly.
        client.ping()
        _redis_client = client
        logger.info("Redis 已连接")
        return _redis_client
    except RedisError:
        logger.exception("Redis 不可用，相关功能将降级")
        _redis_client = None
        return None


def redis_available() -> bool:
    client = get_redis()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except RedisError:
        return False


def redis_status() -> dict:
    """Status payload for health/ready endpoints."""
    if not settings.redis_url:
        return {"configured": False, "ok": False, "detail": "redis_url not set"}
    client = get_redis()
    if client is None:
        return {"configured": True, "ok": False, "detail": "unreachable"}
    try:
        client.ping()
        return {"configured": True, "ok": True, "detail": "pong"}
    except RedisError as exc:
        return {"configured": True, "ok": False, "detail": str(exc)}


def reset_redis_client() -> None:
    """Drop the cached client (tests / config reload)."""
    global _redis_client, _redis_init_attempted
    if _redis_client is not None:
        try:
            _redis_client.close()
        except Exception:
            pass
    _redis_client = None
    _redis_init_attempted = False
