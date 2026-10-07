import json
import logging
from pathlib import Path

from fastapi import APIRouter, Response
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/app", tags=["app"])
APP_STORE_URL = "https://apps.apple.com/cn/app/evenly/id6784235151"
CHANGELOG_PATH = Path(__file__).resolve().parents[1] / "resources" / "ios-changelog.json"
logger = logging.getLogger(__name__)


def _version_parts(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if not 1 <= len(parts) <= 3 or any(not part.isascii() or not part.isdigit() for part in parts):
        return None
    try:
        return tuple(int(part) for part in parts) + (0,) * (3 - len(parts))
    except ValueError:
        return None


def update_message_for_version(version: str) -> str:
    """Use only notes for the published version, never the newest draft."""
    fallback = settings.ios_update_message
    target = _version_parts(version)
    if target is None:
        return fallback
    try:
        entries = json.loads(CHANGELOG_PATH.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            return fallback
        for entry in entries:
            if not isinstance(entry, dict) or _version_parts(entry.get("version")) != target:
                continue
            title = entry.get("title")
            items = entry.get("items")
            if not isinstance(title, str) or not title.strip() or not isinstance(items, list) or not items:
                return fallback
            if any(not isinstance(item, str) or not item.strip() for item in items):
                return fallback
            # A short alert shares the same wording as the full in-app changelog.
            return title.strip() + "\n\n" + "\n".join(f"• {item.strip()}" for item in items[:3])
    except (OSError, ValueError, UnicodeError):
        logger.warning("Could not read iOS changelog; using configured update message")
    return fallback


class IOSUpdateResponse(BaseModel):
    latest_version: str
    message: str
    app_store_url: str
    remind_after_days: int


@router.get("/ios-update", response_model=IOSUpdateResponse)
def get_ios_update(response: Response):
    """Public release metadata; an empty version disables the reminder."""
    response.headers["Cache-Control"] = "no-store"
    return IOSUpdateResponse(
        latest_version=settings.ios_latest_version,
        message=update_message_for_version(settings.ios_latest_version),
        app_store_url=APP_STORE_URL,
        remind_after_days=settings.ios_update_remind_after_days,
    )
