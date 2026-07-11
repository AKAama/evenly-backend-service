"""Tests for Redis-backed rate limits, invitation cache, and APNs token cache."""

from __future__ import annotations

import uuid
from datetime import datetime
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import settings as app_settings
from app.services import invitation_cache, rate_limit, redis_client
from app.services.push import _provider_token
from main import app


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def ping(self):
        return True

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def incr(self, key):
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                deleted += 1
            self.ttls.pop(key, None)
        return deleted

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_redis_state(monkeypatch):
    redis_client.reset_redis_client()
    rate_limit.reset_memory_rate_limits()
    monkeypatch.setattr(app_settings, "redis_url", "redis://fake:6379/0")
    yield
    redis_client.reset_redis_client()
    rate_limit.reset_memory_rate_limits()


def test_rate_limit_with_memory_fallback(monkeypatch):
    monkeypatch.setattr(redis_client, "get_redis", lambda: None)
    assert rate_limit.allow_request("test-bucket", limit=2, window_seconds=60) is True
    assert rate_limit.allow_request("test-bucket", limit=2, window_seconds=60) is True
    assert rate_limit.allow_request("test-bucket", limit=2, window_seconds=60) is False

    rate_limit.enforce_rate_limit("test-bucket-3", limit=1, window_seconds=60)
    with pytest.raises(HTTPException) as exc:
        rate_limit.enforce_rate_limit("test-bucket-3", limit=1, window_seconds=60)
    assert exc.value.status_code == 429


def test_rate_limit_with_fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    assert rate_limit.allow_request("login:ip:1.1.1.1", limit=2, window_seconds=60) is True
    assert rate_limit.allow_request("login:ip:1.1.1.1", limit=2, window_seconds=60) is True
    assert rate_limit.allow_request("login:ip:1.1.1.1", limit=2, window_seconds=60) is False
    assert fake.store["evenly:rl:login:ip:1.1.1.1"] == "3"
    assert fake.ttls["evenly:rl:login:ip:1.1.1.1"] == 60


def test_invitation_cache_roundtrip(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    user_id = uuid.uuid4()
    payload = [
        {
            "id": str(uuid.uuid4()),
            "ledger_id": str(uuid.uuid4()),
            "ledger_name": "Trip",
            "invited_by_name": "Alex",
            "created_at": datetime.utcnow().isoformat(),
        }
    ]
    assert invitation_cache.get_pending_invitations(user_id) is None
    invitation_cache.set_pending_invitations(user_id, payload)
    assert invitation_cache.get_pending_invitations(user_id) == payload
    invitation_cache.invalidate_pending_invitations(user_id)
    assert invitation_cache.get_pending_invitations(user_id) is None


def test_apns_provider_token_is_cached(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)

    mint_calls = {"n": 0}

    def counting_mint():
        mint_calls["n"] += 1
        return "jwt-token-value"

    monkeypatch.setattr("app.services.push._mint_provider_token", counting_mint)

    first = _provider_token()
    second = _provider_token()
    assert first == "jwt-token-value"
    assert second == "jwt-token-value"
    assert mint_calls["n"] == 1
    assert fake.store["evenly:apns:provider_jwt"] == "jwt-token-value"


def test_health_includes_redis_status(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    monkeypatch.setattr(redis_client, "redis_status", lambda: {"configured": True, "ok": True, "detail": "pong"})

    # Avoid DB dependency for readiness — only hit /health
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert "redis" in body
