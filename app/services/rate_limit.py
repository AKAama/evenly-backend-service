"""Simple fixed-window rate limiting with Redis primary and in-memory fallback."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

from fastapi import HTTPException, Request, status
from redis.exceptions import RedisError

from app.services import redis_client

logger = logging.getLogger(__name__)

# bucket -> list of timestamps (seconds)
_memory_hits: dict[str, list[float]] = defaultdict(list)
_memory_lock = threading.Lock()


def _memory_allow(bucket: str, limit: int, window_seconds: int) -> bool:
    now = time.time()
    cutoff = now - window_seconds
    with _memory_lock:
        hits = [ts for ts in _memory_hits[bucket] if ts >= cutoff]
        if len(hits) >= limit:
            _memory_hits[bucket] = hits
            return False
        hits.append(now)
        _memory_hits[bucket] = hits
        return True


def allow_request(bucket: str, *, limit: int, window_seconds: int) -> bool:
    """
    Return True if the request is allowed under the limit.

    Redis path: INCR + EXPIRE on first hit (fixed window).
    Fallback: process-local fixed window when Redis is down/unconfigured.
    """
    if limit <= 0:
        return True

    client = redis_client.get_redis()
    if client is not None:
        key = f"evenly:rl:{bucket}"
        try:
            count = client.incr(key)
            if count == 1:
                client.expire(key, window_seconds)
            if count > limit:
                logger.info("Rate limit hit bucket=%s count=%s limit=%s", bucket, count, limit)
                return False
            return True
        except RedisError:
            logger.exception("Redis rate limit failed; falling back to memory bucket=%s", bucket)

    return _memory_allow(bucket, limit, window_seconds)


def enforce_rate_limit(bucket: str, *, limit: int, window_seconds: int, detail: str = "请求过于频繁，请稍后重试") -> None:
    if not allow_request(bucket, limit=limit, window_seconds=window_seconds):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


def client_ip(request: Request | None) -> str:
    """Best-effort client IP (proxy-aware).

    Order: X-Forwarded-For (first hop) → X-Real-IP → CF-Connecting-IP → ASGI client.
    """
    if request is None:
        return "unknown"

    forwarded = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
    if forwarded:
        # Left-most is the original client when proxies append.
        first = forwarded.split(",")[0].strip()
        if first:
            return first

    for header in ("x-real-ip", "X-Real-IP", "cf-connecting-ip", "CF-Connecting-IP"):
        real = (request.headers.get(header) or "").strip()
        if real:
            return real

    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def reset_memory_rate_limits() -> None:
    with _memory_lock:
        _memory_hits.clear()
