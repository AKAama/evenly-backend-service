import logging
import random
import string
import time
from typing import Dict

from redis import Redis
from redis.exceptions import RedisError

from app.config import settings

logger = logging.getLogger(__name__)

# In-memory fallback for local development when Redis is not configured.
# Format: { email: { code: str, expires_at: float, sent_at: float } }
verification_codes: Dict[str, dict] = {}
_redis_client: Redis | None = None


def generate_code(length: int = 6) -> str:
    """Generate a numeric verification code."""
    return "".join(random.choices(string.digits, k=length))


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _code_key(email: str) -> str:
    return f"evenly:verification:code:{email}"


def _send_lock_key(email: str) -> str:
    return f"evenly:verification:sent:{email}"


def _get_redis_client() -> Redis | None:
    global _redis_client
    if not settings.redis_url:
        return None
    if _redis_client is None:
        _redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _send_with_memory_store(email: str, code: str) -> bool:
    now = time.time()
    existing = verification_codes.get(email)
    if existing and now - existing.get("sent_at", 0) < settings.verification_send_interval_seconds:
        return False

    verification_codes[email] = {
        "code": code,
        "expires_at": now + settings.verification_code_expire_seconds,
        "sent_at": now,
    }
    return True


def _send_with_redis(email: str, code: str) -> bool | None:
    redis_client = _get_redis_client()
    if redis_client is None:
        return None

    try:
        locked = redis_client.set(
            _send_lock_key(email),
            "1",
            ex=settings.verification_send_interval_seconds,
            nx=True,
        )
        if not locked:
            return False

        redis_client.set(
            _code_key(email),
            code,
            ex=settings.verification_code_expire_seconds,
        )
        return True
    except RedisError:
        logger.exception("Redis verification store unavailable; falling back to in-memory store")
        return None


def _verify_with_redis(email: str, code: str) -> bool | None:
    redis_client = _get_redis_client()
    if redis_client is None:
        return None

    try:
        key = _code_key(email)
        stored_code = redis_client.get(key)
        if stored_code != code:
            return False

        redis_client.delete(key)
        redis_client.delete(_send_lock_key(email))
        return True
    except RedisError:
        logger.exception("Redis verification store unavailable; falling back to in-memory store")
        return None


def _verify_with_memory_store(email: str, code: str) -> bool:
    stored = verification_codes.get(email)
    if not stored:
        return False

    if time.time() > stored["expires_at"]:
        verification_codes.pop(email, None)
        return False

    if stored["code"] == code:
        verification_codes.pop(email, None)
        return True

    return False


def send_verification_code(email: str) -> bool:
    """Send a verification code to an email address."""
    normalized_email = _normalize_email(email)
    code = generate_code()

    stored = _send_with_redis(normalized_email, code)
    if stored is None:
        stored = _send_with_memory_store(normalized_email, code)
    if not stored:
        return False

    from app.services.email import get_email_service

    email_service = get_email_service()
    if email_service:
        return email_service.send_verification_code(normalized_email, code)

    logger.warning("Email service is not configured; verification code generated for %s", normalized_email)
    return True


def verify_code(email: str, code: str) -> bool:
    """Verify and consume a verification code."""
    normalized_email = _normalize_email(email)
    result = _verify_with_redis(normalized_email, code)
    if result is not None:
        return result
    return _verify_with_memory_store(normalized_email, code)
