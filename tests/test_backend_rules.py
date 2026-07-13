import asyncio
import json
import logging
import sys
import types
import uuid
import textwrap
from pathlib import Path
from io import BytesIO
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError
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
from app.routers.expenses import (
    confirm_expense,
    create_expense,
    delete_expense,
    get_expenses,
    set_expense_refund,
    update_expense,
)
from app.routers.ledgers import (
    accept_invitation,
    add_member as add_member_endpoint,
    create_ledger,
    get_ledger,
    get_ledger_overview,
    get_ledgers,
    get_members,
    get_or_create_invite_link,
    get_pending_invitations,
    join_via_invite_link,
    preview_invite_link,
    reject_invitation,
    remove_member,
    rotate_invite_link,
)
from app.schemas.ledger import AddMemberRequest
from app.routers import users as users_router
from app.schemas.ledger import LedgerCreate, MemberCreate
from app.routers.settlements import create_settlement, get_settlements
from app.schemas.expense import (
    ConfirmExpenseRequest,
    ExpenseCreate,
    ExpenseRefundRequest,
    ExpenseSplitCreate,
    ExpenseUpdate,
)
from app.schemas.settlement import SettlementCreate
from app.services import verification
from app.services.tencent_asr import TencentASRError, _build_hotword_list, stream_tencent_asr
from app.services.auth import get_password_hash
from app.services.voice_expense import create_voice_expense_draft, parse_expense_draft
from app.routers.expenses import _receive_voice_audio
from app.config import load_settings, settings as app_settings
from main import app


def test_expenses_router_exposes_uuid4_for_voice_session_ids():
    import app.routers.expenses as expenses_router

    assert callable(expenses_router.uuid4)


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
def client(db, monkeypatch):
    monkeypatch.setattr(app_settings, "auth_cookie_name", "evenly_access_token")
    monkeypatch.setattr(app_settings, "auth_cookie_secure", False)
    monkeypatch.setattr(app_settings, "auth_cookie_samesite", "lax")

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


def test_settings_layer_defaults_local_config_and_environment(tmp_path, monkeypatch):
    defaults_path = tmp_path / "config.defaults.yaml"
    config_path = tmp_path / "config.yaml"
    defaults_path.write_text(textwrap.dedent("""
        db:
          host: default-db
          port: 5432
          database: evenly
          user: postgres
          password: postgres
        verification_code_expire_seconds: 600
        verification_send_interval_seconds: 60
        jwt_secret_key: default-secret
        jwt_expire_minutes: 1440
        algorithm: HS256
        auth_cookie_name: evenly_access_token
        auth_cookie_secure: false
        auth_cookie_samesite: lax
        apple_client_id: com.yhma.Evenly
        openai_url: https://api.openai.com/v1/chat/completions
        openai_transcription_model: gpt-4o-mini-transcribe
        openai_text_model: gpt-4o-mini
        asr_engine_model_type: "16k_zh"
        asr_endpoint: "wss://asr.cloud.tencent.com/asr/v2/"
        asr_needvad: 1
        asr_vad_silence_time: 800
        asr_filter_modal: 2
        asr_filter_punc: 0
        asr_convert_num_mode: 1
        asr_hotword_weight: 100
        asr_connect_timeout_seconds: 10
        asr_final_timeout_seconds: 5
        slow_request_threshold_ms: 20.0
    """))
    config_path.write_text(textwrap.dedent("""
        db:
          host: local-db
        jwt_secret_key: local-secret
        auth_cookie_secure: false
        DASHSCOPE_RESPONSES_URL: https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions
        asr_vad_silence_time: 500
        asr_final_timeout_seconds: 3
    """))
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("REDIS_URL", "redis://env-redis:6379/0")

    settings = load_settings(config_path=config_path, defaults_path=defaults_path)

    assert settings.db.host == "local-db"
    assert settings.db.port == 5432
    assert settings.jwt_secret_key == "local-secret"
    assert settings.jwt_expire_minutes == 1440
    assert settings.auth_cookie_secure is True
    assert settings.redis_url == "redis://env-redis:6379/0"
    assert settings.openai_url == "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert settings.asr_vad_silence_time == 500
    assert settings.asr_engine_model_type == "16k_zh"
    assert settings.asr_final_timeout_seconds == 3


def test_apns_private_key_is_loaded_from_path_next_to_config(tmp_path, monkeypatch):
    defaults_path = tmp_path / "config.defaults.yaml"
    config_path = tmp_path / "config.yaml"
    key_path = tmp_path / "AuthKey_TESTKEYID.p8"
    # Build the markers dynamically so secret scanners do not mistake this
    # deliberately invalid fixture for a committed private key.
    key_label = "PRIVATE KEY"
    pem = "\n".join([
        f"-----BEGIN {key_label}-----",
        "not-a-real-apns-private-key",
        f"-----END {key_label}-----",
    ])
    key_path.write_text(pem + "\n", encoding="utf-8")
    defaults_path.write_text(textwrap.dedent("""
        db:
          host: default-db
          port: 5432
          database: evenly
          user: postgres
          password: postgres
        verification_code_expire_seconds: 600
        verification_send_interval_seconds: 60
        jwt_secret_key: default-secret
        jwt_expire_minutes: 1440
        algorithm: HS256
        auth_cookie_name: evenly_access_token
        auth_cookie_secure: false
        auth_cookie_samesite: lax
        apple_client_id: com.yhma.Evenly
        openai_url: https://api.openai.com/v1/chat/completions
        openai_transcription_model: gpt-4o-mini-transcribe
        openai_text_model: gpt-4o-mini
        asr_engine_model_type: "16k_zh"
        asr_endpoint: "wss://asr.cloud.tencent.com/asr/v2/"
        asr_needvad: 1
        asr_vad_silence_time: 800
        asr_filter_modal: 2
        asr_filter_punc: 0
        asr_convert_num_mode: 1
        asr_hotword_weight: 100
        asr_connect_timeout_seconds: 10
        asr_final_timeout_seconds: 5
        slow_request_threshold_ms: 20.0
    """))
    config_path.write_text(textwrap.dedent("""
        apns_team_id: TEAMID1234
        apns_key_id: TESTKEYID
        apns_private_key_path: AuthKey_TESTKEYID.p8
        apns_bundle_id: com.yhma.Evenly
    """))
    monkeypatch.delenv("APNS_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("APNS_PRIVATE_KEY_PATH", raising=False)

    settings = load_settings(config_path=config_path, defaults_path=defaults_path)

    assert settings.apns_team_id == "TEAMID1234"
    assert settings.apns_key_id == "TESTKEYID"
    assert settings.apns_private_key == pem
    assert settings.apns_private_key_path.endswith("AuthKey_TESTKEYID.p8")


def add_member(db, ledger, user):
    db.add(LedgerMember(ledger_id=ledger.id, user_id=user.id, display_name=user.display_name))
    db.commit()


def test_registered_ledger_member_name_follows_current_user_display_name(db):
    user = make_user(db, "live-name@example.com", "旧昵称")
    ledger = Ledger(name="Live names", owner_id=user.id, currency="CNY")
    db.add(ledger)
    db.flush()
    member = LedgerMember(ledger_id=ledger.id, user_id=user.id, display_name="旧昵称")
    db.add(member)
    db.commit()

    user.display_name = "新昵称"
    db.commit()

    assert member.nickname == "新昵称"
    response = get_members(ledger.id, db=db, current_user=user)
    assert response[0].nickname == "新昵称"


def test_temporary_ledger_member_keeps_its_own_name(db):
    owner = make_user(db, "temp-owner@example.com", "Owner")
    ledger = Ledger(name="Temporary names", owner_id=owner.id, currency="CNY")
    db.add(ledger)
    db.flush()
    member = LedgerMember(ledger_id=ledger.id, user_id=None, display_name="临时成员")
    db.add(member)
    db.commit()

    assert member.nickname == "临时成员"
    assert member.temporary_name == "临时成员"


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
    # splits 不在草稿阶段计算，客户端会在提交账单时自行平分
    assert draft["splits"] == []


def test_voice_expense_prompt_sends_members_and_matching_guidance(monkeypatch):
    owner_id = str(uuid.uuid4())
    owner_member_id = str(uuid.uuid4())
    temporary_member_id = str(uuid.uuid4())
    members = [
        {
            "member_id": owner_member_id,
            "user_id": owner_id,
            "name": "我",
            "registered": True,
        },
        {
            "member_id": temporary_member_id,
            "user_id": None,
            "name": "小王",
            "registered": False,
        },
    ]
    captured = {}

    class FakeResponse:
        ok = True
        status_code = 200
        content = b"{}"

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json_text({
                                "title": "住宿",
                                "amount": 500,
                                "currency": "CNY",
                                "category": "住宿",
                                "note": "住宿",
                                "payer_user_id": owner_id,
                                "participant_member_ids": [owner_member_id, temporary_member_id],
                                "confidence": 0.9,
                                "missing_fields": [],
                            }),
                        }
                    }
                ]
            }

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json
        return FakeResponse()

    chat_url = "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
    monkeypatch.setattr(app_settings, "openai_api_key", "test-key")
    monkeypatch.setattr(app_settings, "openai_url", chat_url, raising=False)
    monkeypatch.setattr(app_settings, "openai_text_model", "qwen-plus", raising=False)
    monkeypatch.setattr("app.services.voice_expense.requests.post", fake_post)

    parse_expense_draft("我和小王住宿花了 500，是我付的", members, owner_id)

    payload = captured["payload"]
    system_prompt = payload["messages"][0]["content"]
    user_payload = json.loads(payload["messages"][1]["content"])

    assert captured["url"] == chat_url
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert payload["model"] == "qwen-plus"
    assert payload["response_format"] == {"type": "json_object"}
    assert user_payload["members"] == members
    assert user_payload["current_user_id"] == owner_id
    assert "members.name" in system_prompt
    assert "临时成员" in system_prompt
    assert "没说参与人" in system_prompt
    assert "JSON" in system_prompt


def test_voice_streaming_backend_has_chinese_diagnostic_logs():
    root = Path(__file__).resolve().parents[1]
    expenses_source = (root / "app" / "routers" / "expenses.py").read_text()
    tencent_source = (root / "app" / "services" / "tencent_asr.py").read_text()

    for message in [
        "语音会话已连接",
        "语音会话鉴权失败",
        "语音会话已就绪",
        "语音会话收到停止指令",
        "语音会话识别完成",
        "语音会话生成草稿成功",
        "语音会话失败",
    ]:
        assert message in expenses_source

    for message in [
        "腾讯 ASR 开始连接",
        "腾讯 ASR 已连接",
        "腾讯 ASR 握手成功",
        "腾讯 ASR 收到 final=1",
        "腾讯 ASR 音频发送完成",
        "腾讯 ASR 等待最终结果超时",
    ]:
        assert message in tencent_source


def test_tencent_asr_signature_uses_websocket_url_without_protocol(monkeypatch):
    """签名原文必须是不含 wss:// 的请求 URL，不能使用通用 API canonical request。"""
    import urllib.parse

    import app.services.tencent_asr as tencent_asr_mod

    captured = {}

    class FakeDigest:
        def digest(self):
            return b"signature-bytes"

    def fake_hmac_new(key, message, digestmod):
        captured["message"] = message.decode("utf-8")
        return FakeDigest()

    class FakeUUID:
        int = 42

    monkeypatch.setattr(tencent_asr_mod.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(tencent_asr_mod.uuid, "uuid4", lambda: FakeUUID())
    monkeypatch.setattr(tencent_asr_mod.hmac, "new", fake_hmac_new)

    url = tencent_asr_mod._sign_url(
        endpoint="wss://asr.cloud.tencent.com/asr/v2/",
        appid="1259220000",
        secret_id="test-secret-id",
        secret_key="test-secret-key",
        engine_model_type="16k_zh",
        voice_id="test-voice-id",
        needvad=1,
        vad_silence_time=800,
        filter_modal=2,
        filter_punc=0,
        convert_num_mode=1,
        hotword_list="小王|100",
    )

    unsigned_url = url.split("&signature=", 1)[0].removeprefix("wss://")
    assert captured["message"] == urllib.parse.unquote(unsigned_url)
    assert "hotword_list=小王|100" in captured["message"]
    assert "hotword_list=%E5%B0%8F%E7%8E%8B%7C100" in unsigned_url
    assert not captured["message"].startswith("GET\n")


def test_tencent_asr_hotword_list_uses_weight_by_name_type():
    assert _build_hotword_list(
        ["111", "陈皖琼", "Sylvia", "Stella", "陈皖琼", "带 空格", "超过十个汉字的成员名字甲乙丙"],
        100,
    ) == "陈皖琼|100,Sylvia|11,Stella|11"
    assert _build_hotword_list(["陈皖琼", "Sylvia"], 10) == "陈皖琼|10,Sylvia|10"


@pytest.mark.asyncio
async def test_tencent_asr_stream_protocol(monkeypatch):
    """腾讯实时 ASR 流程：
      1. URL 带签名参数（HMAC-SHA1）连接 wss://...
      2. 服务端先发 handshake {"code":0,"message":"success",...}
      3. 客户端送 binary 音频帧
      4. 服务端回 result 消息：slice_type=0/1 是 partial，slice_type=2 是 final
      5. 客户端音频送完后发 {"type":"end"} 文本帧
      6. 服务端发 {"final":1} 后关闭
    """
    class FakeTencentWebSocket:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.sent_messages = []
            self.recv_messages: asyncio.Queue = asyncio.Queue()
            # 服务端响应序列：握手 → partial → final → final=1 结束
            self.recv_messages.put_nowait(json_text({"code": 0, "message": "success", "voice_id": "v-1"}))
            self.recv_messages.put_nowait(json_text({
                "code": 0, "message": "success", "voice_id": "v-1",
                "result": {
                    "slice_type": 1, "index": 0, "start_time": 0, "end_time": 1200,
                    "voice_text_str": "午饭", "word_size": 0, "word_list": [],
                },
            }))
            self.recv_messages.put_nowait(json_text({
                "code": 0, "message": "success", "voice_id": "v-1",
                "result": {
                    "slice_type": 2, "index": 0, "start_time": 0, "end_time": 2400,
                    "voice_text_str": "午饭八十八元", "word_size": 0, "word_list": [],
                },
            }))
            self.recv_messages.put_nowait(json_text({"code": 0, "message": "success", "final": 1}))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send(self, message):
            self.sent_messages.append(message)

        async def recv(self):
            return await self.recv_messages.get()

        async def close(self):
            pass

    import app.services.tencent_asr as tencent_asr_mod
    captured: dict = {}
    def fake_connect(url, **kwargs):
        ws = FakeTencentWebSocket(url, **kwargs)
        captured["ws"] = ws
        return ws
    monkeypatch.setattr(tencent_asr_mod.websockets, "connect", fake_connect, raising=False)

    monkeypatch.setattr(app_settings, "asr_appid", "12345", raising=False)
    monkeypatch.setattr(app_settings, "asr_secret_id", "sid", raising=False)
    monkeypatch.setattr(app_settings, "asr_secret_key", "skey", raising=False)
    monkeypatch.setattr(app_settings, "asr_engine_model_type", "16k_zh", raising=False)
    monkeypatch.setattr(app_settings, "asr_endpoint", "wss://asr.cloud.tencent.com/asr/v2/", raising=False)
    monkeypatch.setattr(app_settings, "asr_needvad", 1, raising=False)
    monkeypatch.setattr(app_settings, "asr_vad_silence_time", 800, raising=False)
    monkeypatch.setattr(app_settings, "asr_filter_modal", 2, raising=False)
    monkeypatch.setattr(app_settings, "asr_filter_punc", 0, raising=False)
    monkeypatch.setattr(app_settings, "asr_convert_num_mode", 1, raising=False)
    monkeypatch.setattr(app_settings, "asr_hotword_weight", 100, raising=False)
    monkeypatch.setattr(app_settings, "asr_connect_timeout_seconds", 5, raising=False)
    monkeypatch.setattr(app_settings, "asr_final_timeout_seconds", 5, raising=False)

    async def chunks():
        yield b"audio-one"
        yield b"audio-two"

    events = []
    async for event in stream_tencent_asr(
        chunks(), wav_name="ledger-id", session_id="test-session",
        hotwords=["小王", "张三"],
    ):
        events.append(event)

    ws = captured["ws"]
    # 发送帧包含两段 audio binary + 末尾的 {"type":"end"} 文本帧
    audio_frames = [m for m in ws.sent_messages if isinstance(m, (bytes, bytearray))]
    assert b"audio-one" in audio_frames
    assert b"audio-two" in audio_frames
    end_msg = next(m for m in ws.sent_messages if isinstance(m, str) and "end" in m)
    assert json.loads(end_msg) == {"type": "end"}
    # 签名 URL 必须包含必要的鉴权参数和 hotword_list（包含我们传的人名）
    assert "secretid=sid" in ws.url
    assert "engine_model_type=16k_zh" in ws.url
    assert "voice_format=1" in ws.url
    assert "sample_rate=16000" in ws.url
    assert "hotword_list=" in ws.url
    # weight=100 表示同音字替换（人名场景）
    assert "%E5%B0%8F%E7%8E%8B%7C100" in ws.url  # 小王|100 URL-encoded
    # 事件顺序：partial → final
    assert events == [
        {"type": "partial", "text": "午饭", "is_final": False},
        {"type": "final", "text": "午饭八十八元", "is_final": True},
    ]


@pytest.mark.asyncio
async def test_voice_receiver_accepts_start_control_message_without_unknown_warning(caplog):
    class FakeWebSocket:
        def __init__(self):
            self.messages = [
                {
                    "text": json_text({
                        "type": "start",
                        "audio": {
                            "format": "pcm_s16le",
                            "sample_rate": 16000,
                            "channels": 1,
                        },
                    })
                },
                {"text": json_text({"type": "stop"})},
            ]

        async def receive(self):
            return self.messages.pop(0)

    caplog.set_level(logging.WARNING)
    queue = asyncio.Queue()
    stats = {"chunks": 0, "bytes": 0}

    await _receive_voice_audio(
        FakeWebSocket(),
        queue,
        session_id="test-session",
        audio_stats=stats,
    )

    assert await queue.get() is None
    assert "语音会话收到未知控制消息" not in caplog.text


@pytest.mark.asyncio
async def test_tencent_asr_times_out_after_audio_end_without_final(monkeypatch):
    """发送完 {"type":"end"} 后如果服务端一直不回 final=1，要超时收尾，不能挂死。"""
    class HangingTencentWebSocket:
        def __init__(self, *a, **kw):
            self.sent_messages = []
            self.handshake_sent = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send(self, message):
            self.sent_messages.append(message)

        async def recv(self):
            if not self.handshake_sent:
                self.handshake_sent = True
                return json_text({"code": 0, "message": "success", "voice_id": "v"})
            await asyncio.sleep(3600)

        async def close(self):
            pass

    import app.services.tencent_asr as tencent_asr_mod
    monkeypatch.setattr(tencent_asr_mod.websockets, "connect",
                        lambda *a, **kw: HangingTencentWebSocket(*a, **kw), raising=False)
    monkeypatch.setattr(app_settings, "asr_appid", "123", raising=False)
    monkeypatch.setattr(app_settings, "asr_secret_id", "sid", raising=False)
    monkeypatch.setattr(app_settings, "asr_secret_key", "skey", raising=False)
    monkeypatch.setattr(app_settings, "asr_engine_model_type", "16k_zh", raising=False)
    monkeypatch.setattr(app_settings, "asr_endpoint", "wss://asr.example/asr/v2/", raising=False)
    monkeypatch.setattr(app_settings, "asr_connect_timeout_seconds", 5, raising=False)
    monkeypatch.setattr(app_settings, "asr_final_timeout_seconds", 0.05, raising=False)
    monkeypatch.setattr(app_settings, "asr_needvad", 1, raising=False)
    monkeypatch.setattr(app_settings, "asr_vad_silence_time", 800, raising=False)
    monkeypatch.setattr(app_settings, "asr_filter_modal", 0, raising=False)
    monkeypatch.setattr(app_settings, "asr_filter_punc", 0, raising=False)
    monkeypatch.setattr(app_settings, "asr_convert_num_mode", 1, raising=False)
    monkeypatch.setattr(app_settings, "asr_hotword_weight", 10, raising=False)

    async def chunks():
        yield b"audio"

    async def collect_stream():
        async for _ in stream_tencent_asr(chunks(), wav_name="test", session_id="test-session"):
            pass

    # 不抛异常（超时后优雅结束，而不是报错），0.5s 内能跑完
    await asyncio.wait_for(collect_stream(), timeout=0.5)


def json_text(payload):
    import json

    return json.dumps(payload)


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


def test_reject_invitation_keeps_rejected_membership_row(db):
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
    invitation_id = invitation.id

    reject_invitation(invitation_id, db=db, current_user=friend)

    membership = db.query(LedgerMember).filter(LedgerMember.id == invitation_id).one()
    assert membership.status == "rejected"
    assert get_pending_invitations(db=db, current_user=friend) == []
    assert get_ledgers(db=db, current_user=friend) == []

    # Owner can still see the declined invite on the ledger.
    detail = get_ledger(response.id, db=db, current_user=owner)
    statuses = {m.user_id: m.status for m in detail.members}
    assert statuses[friend.id] == "rejected"


def test_reinvite_rejected_member_sets_pending_again(db):
    owner = make_user(db, "owner@example.com", "Owner")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    membership = LedgerMember(
        ledger_id=ledger.id,
        user_id=friend.id,
        status="rejected",
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)
    original_id = membership.id

    reinvited = add_member_endpoint(
        ledger.id,
        AddMemberRequest(user_id=friend.id, is_temporary=False),
        db=db,
        current_user=owner,
    )

    assert reinvited.id == original_id
    assert reinvited.status == "pending"
    db.refresh(membership)
    assert membership.status == "pending"
    pending = get_pending_invitations(db=db, current_user=friend)
    assert len(pending) == 1
    assert pending[0].id == original_id

    # Pending invite cannot be invited again.
    with pytest.raises(HTTPException) as exc_info:
        add_member_endpoint(
            ledger.id,
            AddMemberRequest(user_id=friend.id, is_temporary=False),
            db=db,
            current_user=owner,
        )
    assert_http_error(exc_info, 400)

    accept_invitation(original_id, db=db, current_user=friend)
    db.refresh(membership)
    assert membership.status == "active"
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


@pytest.mark.parametrize(
    ("icon_type", "icon_value"),
    [("sf_symbol", "fork.knife"), ("emoji", "🍜")],
)
def test_expense_category_icon_is_persisted(db, icon_type, icon_value):
    owner = make_user(db, f"{icon_type}@example.com", "Owner")
    ledger = make_ledger(db, owner)

    expense = create_expense(
        ledger.id,
        ExpenseCreate(
            title="午餐",
            total_amount=Decimal("20.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[ExpenseSplitCreate(user_id=owner.id, amount=Decimal("20.00"))],
            category="餐饮",
            icon_type=icon_type,
            icon_value=icon_value,
        ),
        db=db,
        current_user=owner,
    )

    assert expense.category == "餐饮"
    assert expense.icon_type == icon_type
    assert expense.icon_value == icon_value

    # List + overview must surface icons (regression: constructors dropped them → app ¥ fallback).
    listed = get_expenses(ledger.id, db=db, current_user=owner)
    assert len(listed) == 1
    assert listed[0].category == "餐饮"
    assert listed[0].icon_type == icon_type
    assert listed[0].icon_value == icon_value

    overview = get_ledger_overview(ledger.id, db=db, current_user=owner)
    assert len(overview.expenses) == 1
    assert overview.expenses[0].category == "餐饮"
    assert overview.expenses[0].icon_type == icon_type
    assert overview.expenses[0].icon_value == icon_value


def test_expense_without_category_icon_remains_compatible():
    payload = ExpenseCreate(
        title="旧账单",
        total_amount=Decimal("10.00"),
        expense_date=date.today(),
        payer_id=uuid.uuid4(),
        splits=[ExpenseSplitCreate(user_id=uuid.uuid4(), amount=Decimal("10.00"))],
    )

    assert payload.category is None
    assert payload.icon_type is None
    assert payload.icon_value is None


def test_ledger_invite_link_qr_join_flow(db):
    owner = make_user(db, "qr-owner@example.com", "Owner")
    friend = make_user(db, "qr-friend@example.com", "Friend")
    stranger = make_user(db, "qr-stranger@example.com", "Stranger")
    ledger = make_ledger(db, owner)

    link = get_or_create_invite_link(ledger.id, db=db, current_user=owner)
    again = get_or_create_invite_link(ledger.id, db=db, current_user=owner)
    assert link.token == again.token
    assert link.url.endswith(f"/join/{link.token}")

    preview = preview_invite_link(link.token, db=db)
    assert preview.valid is True
    assert preview.ledger_name == ledger.name
    assert preview.owner_name == "Owner"

    joined = join_via_invite_link(link.token, db=db, current_user=friend)
    assert joined.status == "active"
    assert joined.ledger_id == ledger.id

    already = join_via_invite_link(link.token, db=db, current_user=friend)
    assert already.status == "already_member"

    # Non-owner cannot mint invite links.
    with pytest.raises(HTTPException) as forbidden:
        get_or_create_invite_link(ledger.id, db=db, current_user=stranger)
    assert forbidden.value.status_code == 403

    rotated = rotate_invite_link(ledger.id, db=db, current_user=owner)
    assert rotated.token != link.token
    with pytest.raises(HTTPException) as invalid:
        preview_invite_link(link.token, db=db)
    assert invalid.value.status_code == 404

    join_via_invite_link(rotated.token, db=db, current_user=stranger)
    members = get_members(ledger.id, db=db, current_user=owner)
    active_user_ids = {m.user_id for m in members if m.status == "active"}
    assert owner.id in active_user_ids
    assert friend.id in active_user_ids
    assert stranger.id in active_user_ids


@pytest.mark.parametrize(
    ("icon_type", "icon_value"),
    [("unknown", "fork.knife"), ("sf_symbol", "not.allowed"), ("emoji", "🍜🍚"), (None, "🍜")],
)
def test_invalid_expense_icon_is_rejected(icon_type, icon_value):
    with pytest.raises(ValidationError):
        ExpenseCreate(
            title="Invalid icon",
            total_amount=Decimal("10.00"),
            expense_date=date.today(),
            payer_id=uuid.uuid4(),
            splits=[ExpenseSplitCreate(user_id=uuid.uuid4(), amount=Decimal("10.00"))],
            category="餐饮",
            icon_type=icon_type,
            icon_value=icon_value,
        )


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


def test_partial_refund_reduces_settlement_to_net(db):
    """Hotel 600, refund 100 → effective 500 split A/B equally → each 250."""
    from app.services.settlement import SettlementCalculator, expense_net_amount

    a = make_user(db, "refund-a@example.com", "A")
    b = make_user(db, "refund-b@example.com", "B")
    ledger = make_ledger(db, a)
    add_member(db, ledger, b)

    expense = create_expense(
        ledger.id,
        ExpenseCreate(
            title="住宿",
            total_amount=Decimal("600.00"),
            expense_date=date.today(),
            payer_id=a.id,
            splits=[
                ExpenseSplitCreate(user_id=a.id, amount=Decimal("300.00")),
                ExpenseSplitCreate(user_id=b.id, amount=Decimal("300.00")),
            ],
        ),
        db=db,
        current_user=a,
    )
    confirm_expense(expense.id, ConfirmExpenseRequest(status="confirmed"), db=db, current_user=b)

    updated = set_expense_refund(
        expense.id,
        ExpenseRefundRequest(refund_amount=Decimal("100.00"), note="房型变更"),
        db=db,
        current_user=a,
    )
    assert updated.refund_amount == Decimal("100.00")
    assert expense_net_amount(updated) == Decimal("500.00")
    assert updated.total_amount == Decimal("600.00")

    calc = SettlementCalculator(db, ledger.id)
    balances = calc.calculate_net_balances()
    # A paid net 500, owes 250 → +250; B owes 250 → -250
    assert balances[a.id] == Decimal("250.00")
    assert balances[b.id] == Decimal("-250.00")
    settlements = calc.calculate_settlements()
    assert len(settlements) == 1
    assert settlements[0]["from_user_id"] == b.id
    assert settlements[0]["to_user_id"] == a.id
    assert settlements[0]["amount"] == Decimal("250.00")


def test_update_pending_expense_resets_confirmations(db):
    owner = make_user(db, "edit-owner@example.com", "Owner")
    friend = make_user(db, "edit-friend@example.com", "Friend")
    other = make_user(db, "edit-other@example.com", "Other")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)
    add_member(db, ledger, other)

    expense = create_expense(
        ledger.id,
        ExpenseCreate(
            title="Dinner",
            total_amount=Decimal("30.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[
                ExpenseSplitCreate(user_id=owner.id, amount=Decimal("10.00")),
                ExpenseSplitCreate(user_id=friend.id, amount=Decimal("10.00")),
                ExpenseSplitCreate(user_id=other.id, amount=Decimal("10.00")),
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
    assert db.query(Expense).filter(Expense.id == expense.id).one().status == ExpenseStatus.PENDING

    updated = update_expense(
        expense.id,
        ExpenseUpdate(
            title="Dinner edited",
            total_amount=Decimal("45.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[
                ExpenseSplitCreate(user_id=owner.id, amount=Decimal("15.00")),
                ExpenseSplitCreate(user_id=friend.id, amount=Decimal("15.00")),
                ExpenseSplitCreate(user_id=other.id, amount=Decimal("15.00")),
            ],
            category="餐饮",
            icon_type="emoji",
            icon_value="🍜",
        ),
        db=db,
        current_user=owner,
    )
    assert updated.title == "Dinner edited"
    assert updated.total_amount == Decimal("45.00")
    assert updated.category == "餐饮"
    assert updated.icon_value == "🍜"
    assert updated.status == ExpenseStatus.PENDING.value

    confs = db.query(ExpenseConfirmation).filter(ExpenseConfirmation.expense_id == expense.id).all()
    assert {c.user_id for c in confs} == {owner.id}
    splits = db.query(ExpenseSplit).filter(ExpenseSplit.expense_id == expense.id).all()
    assert len(splits) == 3
    assert sum(s.amount for s in splits) == Decimal("45.00")


def test_update_expense_rejects_non_creator_and_confirmed(db):
    owner = make_user(db, "edit2-owner@example.com", "Owner")
    friend = make_user(db, "edit2-friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, friend)

    expense = create_expense(
        ledger.id,
        ExpenseCreate(
            title="Solo",
            total_amount=Decimal("20.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[ExpenseSplitCreate(user_id=owner.id, amount=Decimal("20.00"))],
        ),
        db=db,
        current_user=owner,
    )
    # Only owner in splits → auto-confirmed
    assert expense.status == ExpenseStatus.CONFIRMED

    payload = ExpenseUpdate(
        title="Nope",
        total_amount=Decimal("25.00"),
        expense_date=date.today(),
        payer_id=owner.id,
        splits=[ExpenseSplitCreate(user_id=owner.id, amount=Decimal("25.00"))],
    )
    with pytest.raises(HTTPException) as confirmed_err:
        update_expense(expense.id, payload, db=db, current_user=owner)
    assert confirmed_err.value.status_code == 400

    pending = create_expense(
        ledger.id,
        ExpenseCreate(
            title="Shared",
            total_amount=Decimal("20.00"),
            expense_date=date.today(),
            payer_id=owner.id,
            splits=[
                ExpenseSplitCreate(user_id=owner.id, amount=Decimal("10.00")),
                ExpenseSplitCreate(user_id=friend.id, amount=Decimal("10.00")),
            ],
        ),
        db=db,
        current_user=owner,
    )
    with pytest.raises(HTTPException) as forbidden:
        update_expense(
            pending.id,
            ExpenseUpdate(
                title="Hijack",
                total_amount=Decimal("20.00"),
                expense_date=date.today(),
                payer_id=owner.id,
                splits=[
                    ExpenseSplitCreate(user_id=owner.id, amount=Decimal("10.00")),
                    ExpenseSplitCreate(user_id=friend.id, amount=Decimal("10.00")),
                ],
            ),
            db=db,
            current_user=friend,
        )
    assert forbidden.value.status_code == 403


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


def test_payer_does_not_need_to_confirm_expense(db):
    owner = make_user(db, "owner@example.com", "Owner")
    payer = make_user(db, "payer@example.com", "Payer")
    friend = make_user(db, "friend@example.com", "Friend")
    ledger = make_ledger(db, owner)
    add_member(db, ledger, payer)
    add_member(db, ledger, friend)

    # Creator is owner, payer is someone else; friend still must confirm.
    expense = create_expense(
        ledger.id,
        ExpenseCreate(
            title="Taxi",
            total_amount=Decimal("30.00"),
            expense_date=date.today(),
            payer_id=payer.id,
            splits=[
                ExpenseSplitCreate(user_id=owner.id, amount=Decimal("10.00")),
                ExpenseSplitCreate(user_id=payer.id, amount=Decimal("10.00")),
                ExpenseSplitCreate(user_id=friend.id, amount=Decimal("10.00")),
            ],
        ),
        db=db,
        current_user=owner,
    )

    auto_confirmed = {
        row.user_id
        for row in db.query(ExpenseConfirmation).filter(
            ExpenseConfirmation.expense_id == expense.id,
            ExpenseConfirmation.status == "confirmed",
        ).all()
    }
    assert owner.id in auto_confirmed
    assert payer.id in auto_confirmed
    assert friend.id not in auto_confirmed
    assert expense.status == ExpenseStatus.PENDING

    with pytest.raises(HTTPException) as exc_info:
        confirm_expense(
            expense.id,
            ConfirmExpenseRequest(status="confirmed"),
            db=db,
            current_user=payer,
        )
    assert_http_error(exc_info, 400)

    confirm_expense(
        expense.id,
        ConfirmExpenseRequest(status="confirmed"),
        db=db,
        current_user=friend,
    )
    db.refresh(expense)
    assert expense.status == ExpenseStatus.CONFIRMED


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
    monkeypatch.setattr("app.services.redis_client.get_redis", lambda: None)
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
    monkeypatch.setattr("app.services.redis_client.get_redis", lambda: None)
    monkeypatch.setattr(verification, "generate_code", lambda length=6: "654321")
    monkeypatch.setattr("app.services.email.get_email_service", lambda: None)

    assert verification.send_verification_code("user@example.com") is True
    assert verification.verify_code("user@example.com", "654321") is False


def test_verification_codes_are_isolated_by_purpose(monkeypatch):
    verification.verification_codes.clear()
    monkeypatch.setattr(verification.settings, "redis_url", None)
    monkeypatch.setattr("app.services.redis_client.get_redis", lambda: None)
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
    monkeypatch.setattr("app.services.redis_client.get_redis", lambda: None)
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
