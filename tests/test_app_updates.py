import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import load_settings
from app.routers import app_updates
from main import app


def test_update_metadata_is_public_and_disabled_by_default(monkeypatch):
    monkeypatch.setattr(app_updates.settings, "ios_latest_version", "")
    with TestClient(app) as client:
        response = client.get("/app/ios-update")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["latest_version"] == ""
    assert response.json()["app_store_url"] == "https://apps.apple.com/cn/app/evenly/id6784235151"


def test_update_metadata_returns_configured_release(monkeypatch):
    monkeypatch.setattr(app_updates.settings, "ios_latest_version", "1.10.0")
    monkeypatch.setattr(app_updates.settings, "ios_update_message", "支持纠正误拒绝账单。")
    monkeypatch.setattr(app_updates.settings, "ios_update_remind_after_days", 3)
    with TestClient(app) as client:
        response = client.get("/app/ios-update")
    assert response.json() == {
        "latest_version": "1.10.0",
        "message": "支持纠正误拒绝账单。",
        "app_store_url": app_updates.APP_STORE_URL,
        "remind_after_days": 3,
    }


@pytest.mark.parametrize("version", ["1.2-beta", "abc", "1..2", "1.2.3.4"])
def test_update_config_rejects_invalid_versions(monkeypatch, version):
    monkeypatch.setenv("IOS_LATEST_VERSION", version)
    with pytest.raises(ValidationError):
        load_settings()


def test_update_config_reads_environment(monkeypatch):
    monkeypatch.setenv("IOS_LATEST_VERSION", "1.10.0")
    monkeypatch.setenv("IOS_UPDATE_REMIND_AFTER_DAYS", "5")
    settings = load_settings()
    assert settings.ios_latest_version == "1.10.0"
    assert settings.ios_update_remind_after_days == 5


def test_update_uses_matching_json_notes_instead_of_newest_draft(monkeypatch, tmp_path):
    notes = tmp_path / "changelog.json"
    notes.write_text(json.dumps([
        {"version": "1.0.3", "title": "未发布版本", "items": ["不能提前提示"]},
        {"version": "1.0.2", "title": "凭据与账本管理", "items": ["凭据", "确认", "封面", "更多优化"]},
    ], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(app_updates, "CHANGELOG_PATH", notes)
    monkeypatch.setattr(app_updates.settings, "ios_latest_version", "1.0.2")
    monkeypatch.setattr(app_updates.settings, "ios_update_message", "旧配置文案")
    with TestClient(app) as client:
        response = client.get("/app/ios-update")
    assert response.status_code == 200
    assert response.json()["latest_version"] == "1.0.2"
    assert response.json()["message"] == "凭据与账本管理\n\n• 凭据\n• 确认\n• 封面"


def test_json_changelog_does_not_enable_an_unpublished_release(monkeypatch):
    monkeypatch.setattr(app_updates.settings, "ios_latest_version", "")
    monkeypatch.setattr(app_updates.settings, "ios_update_message", "默认提示")
    with TestClient(app) as client:
        response = client.get("/app/ios-update")
    assert response.json()["latest_version"] == ""
    assert response.json()["message"] == "默认提示"


def test_json_version_matching_uses_numeric_components(monkeypatch, tmp_path):
    notes = tmp_path / "changelog.json"
    notes.write_text(json.dumps([
        {"version": None, "title": "历史记录", "items": ["无版本"]},
        {"version": "1.0", "title": "已发布", "items": ["新功能"]},
    ]), encoding="utf-8")
    monkeypatch.setattr(app_updates, "CHANGELOG_PATH", notes)
    assert app_updates.update_message_for_version("1.0.0") == "已发布\n\n• 新功能"


@pytest.mark.parametrize("content", [
    None, "not json", "{}", "[]",
    '[{"version":"9.9","title":"其他版本","items":["不能混用"]}]',
    '[{"version":"1.0.2","title":"错误内容","items":[null]}]',
    '[{"version":"1.0.2","title":"","items":["功能"]}]',
    '[{"version":"1.0.2","title":"标题","items":[]}]',
])
def test_missing_or_invalid_json_falls_back_without_breaking_update(monkeypatch, tmp_path, content):
    notes = tmp_path / "changelog.json"
    if content is not None:
        notes.write_text(content, encoding="utf-8")
    monkeypatch.setattr(app_updates, "CHANGELOG_PATH", notes)
    monkeypatch.setattr(app_updates.settings, "ios_latest_version", "1.0.2")
    monkeypatch.setattr(app_updates.settings, "ios_update_message", "安全的备用文案")
    with TestClient(app) as client:
        response = client.get("/app/ios-update")
    assert response.status_code == 200
    assert response.json()["message"] == "安全的备用文案"


def test_backend_bundle_contains_ios_changelog_for_current_release(monkeypatch):
    monkeypatch.setattr(app_updates.settings, "ios_update_message", "备用")
    entries = json.loads(app_updates.CHANGELOG_PATH.read_text(encoding="utf-8"))
    latest = entries[0]
    assert app_updates._version_parts(latest["version"]) is not None
    assert app_updates.update_message_for_version(latest["version"]).startswith(latest["title"] + "\n\n• ")
import json
