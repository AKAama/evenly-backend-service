import logging
import random
import string
import time
from typing import Dict

from redis.exceptions import RedisError

from app.config import settings
from app.services import redis_client

logger = logging.getLogger(__name__)

# In-memory fallback for local development when Redis is not configured.
# Format: { email: { code: str, expires_at: float, sent_at: float } }
verification_codes: Dict[str, dict] = {}


def generate_code(length: int = 6) -> str:
    """Generate a numeric verification code."""
    return "".join(random.choices(string.digits, k=length))


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _code_key(email: str, purpose: str) -> str:
    return f"evenly:verification:{purpose}:code:{email}"


def _send_lock_key(email: str, purpose: str) -> str:
    return f"evenly:verification:{purpose}:sent:{email}"


def _send_with_memory_store(email: str, code: str, purpose: str) -> bool:
    now = time.time()
    key = f"{purpose}:{email}"
    existing = verification_codes.get(key)
    if existing and now - existing.get("sent_at", 0) < settings.verification_send_interval_seconds:
        return False

    verification_codes[key] = {
        "code": code,
        "expires_at": now + settings.verification_code_expire_seconds,
        "sent_at": now,
    }
    return True


def _send_with_redis(email: str, code: str, purpose: str) -> bool | None:
    client = redis_client.get_redis()
    if client is None:
        return None

    try:
        locked = client.set(
            _send_lock_key(email, purpose),
            "1",
            ex=settings.verification_send_interval_seconds,
            nx=True,
        )
        if not locked:
            return False

        client.set(
            _code_key(email, purpose),
            code,
            ex=settings.verification_code_expire_seconds,
        )
        return True
    except RedisError:
        logger.exception("Redis 验证码存储不可用，回退内存")
        return None


def _verify_with_redis(email: str, code: str, purpose: str) -> bool | None:
    client = redis_client.get_redis()
    if client is None:
        return None

    try:
        key = _code_key(email, purpose)
        stored_code = client.get(key)
        if stored_code != code:
            return False

        client.delete(key)
        client.delete(_send_lock_key(email, purpose))
        return True
    except RedisError:
        logger.exception("Redis 验证码存储不可用，回退内存")
        return None


def _verify_with_memory_store(email: str, code: str, purpose: str) -> bool:
    key = f"{purpose}:{email}"
    stored = verification_codes.get(key)
    if not stored:
        return False

    if time.time() > stored["expires_at"]:
        verification_codes.pop(key, None)
        return False

    if stored["code"] == code:
        verification_codes.pop(key, None)
        return True

    return False


def send_verification_code(email: str, purpose: str = "register") -> bool:
    """Send a verification code to an email address."""
    normalized_email = _normalize_email(email)
    code = generate_code()

    stored = _send_with_redis(normalized_email, code, purpose)
    if stored is None:
        stored = _send_with_memory_store(normalized_email, code, purpose)
    if not stored:
        return False

    from app.services.email import get_email_service

    email_service = get_email_service()
    if email_service:
        return email_service.send_verification_code(normalized_email, code)

    logger.warning("邮件服务未配置，验证码已生成但未发送 email=%s", normalized_email)
    return True


def verify_code(email: str, code: str, purpose: str = "register") -> bool:
    """Verify and consume a verification code."""
    normalized_email = _normalize_email(email)
    result = _verify_with_redis(normalized_email, code, purpose)
    if result is not None:
        return result
    return _verify_with_memory_store(normalized_email, code, purpose)
