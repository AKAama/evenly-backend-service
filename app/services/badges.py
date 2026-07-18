"""User nameplates (铭牌). Definitions live in DB; admins manage them."""

from __future__ import annotations

import re
import uuid
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.badge import Badge

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

# key → (label, color); refreshed after admin mutations or on first read.
_meta_cache: dict[str, tuple[str | None, str | None]] | None = None


def invalidate_badge_cache() -> None:
    global _meta_cache
    _meta_cache = None


def _load_cache(db: Session | None = None) -> dict[str, tuple[str | None, str | None]]:
    global _meta_cache
    if _meta_cache is not None:
        return _meta_cache
    if db is not None:
        rows = list_badges(db, active_only=False)
        _meta_cache = {b.key: (b.label, b.color) for b in rows}
        return _meta_cache
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        rows = list_badges(session, active_only=False)
        _meta_cache = {b.key: (b.label, b.color) for b in rows}
        return _meta_cache
    finally:
        session.close()


def slugify_key(label: str) -> str:
    """Fallback key from label — prefer admin-supplied key."""
    raw = (label or "").strip().lower()
    ascii_part = re.sub(r"[^a-z0-9]+", "_", raw)
    ascii_part = re.sub(r"_+", "_", ascii_part).strip("_")
    if ascii_part and _KEY_RE.match(ascii_part):
        return ascii_part[:32]
    return f"b_{uuid.uuid4().hex[:10]}"


def list_badges(db: Session, *, active_only: bool = False) -> list[Badge]:
    q = db.query(Badge)
    if active_only:
        q = q.filter(Badge.is_active.is_(True))
    return q.order_by(Badge.sort_order.asc(), Badge.created_at.asc()).all()


def get_badge_by_key(db: Session, key: str) -> Badge | None:
    if not key:
        return None
    return db.query(Badge).filter(Badge.key == key.strip().lower()).first()


def normalize_badge(db: Session, value: str | None) -> str | None:
    """Return a valid active badge key or None (clear)."""
    if value is None:
        return None
    key = str(value).strip().lower()
    if not key:
        return None
    row = get_badge_by_key(db, key)
    if row is None or not row.is_active:
        raise ValueError(f"未知或已停用的铭牌: {key}")
    return row.key


def badge_label(key: str | None, db: Session | None = None) -> str | None:
    if not key:
        return None
    label, _ = _load_cache(db).get(key, (None, None))
    return label


def badge_color(key: str | None, db: Session | None = None) -> str | None:
    if not key:
        return None
    _, color = _load_cache(db).get(key, (None, None))
    return color


def create_badge(
    db: Session,
    *,
    label: str,
    description: str | None = None,
    color: str = "blue",
    key: str | None = None,
    sort_order: int | None = None,
) -> Badge:
    label = (label or "").strip()
    if not label:
        raise ValueError("铭牌名称不能为空")
    if len(label) > 40:
        raise ValueError("铭牌名称最多 40 字")

    raw_key = (key or "").strip().lower() or slugify_key(label)
    if not _KEY_RE.match(raw_key):
        raise ValueError("标识 key 需为 2–31 位小写英文/数字/下划线，且以字母开头")
    if get_badge_by_key(db, raw_key):
        raise ValueError(f"标识已存在: {raw_key}")

    if sort_order is None:
        max_order = db.query(Badge).count()
        sort_order = (max_order + 1) * 10

    row = Badge(
        id=uuid.uuid4(),
        key=raw_key,
        label=label,
        description=(description or "").strip() or None,
        color=(color or "blue").strip()[:32],
        sort_order=int(sort_order),
        is_active=True,
    )
    db.add(row)
    db.flush()
    invalidate_badge_cache()
    return row


def update_badge(
    db: Session,
    badge_id: UUID,
    *,
    label: str | None = None,
    description=...,
    color: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
) -> Badge:
    row = db.query(Badge).filter(Badge.id == badge_id).first()
    if not row:
        raise LookupError("铭牌不存在")
    if label is not None:
        label = label.strip()
        if not label:
            raise ValueError("铭牌名称不能为空")
        row.label = label[:40]
    if description is not ...:
        if description is None or str(description).strip() == "":
            row.description = None
        else:
            row.description = str(description).strip()[:200]
    if color is not None:
        row.color = color.strip()[:32] or "blue"
    if sort_order is not None:
        row.sort_order = int(sort_order)
    if is_active is not None:
        row.is_active = bool(is_active)
    db.flush()
    invalidate_badge_cache()
    return row


def delete_badge(db: Session, badge_id: UUID, *, user_model) -> str:
    """Delete definition; clear users holding this badge. Returns deleted key."""
    row = db.query(Badge).filter(Badge.id == badge_id).first()
    if not row:
        raise LookupError("铭牌不存在")
    key = row.key
    db.query(user_model).filter(user_model.badge == key).update(
        {"badge": None}, synchronize_session=False
    )
    db.delete(row)
    db.flush()
    invalidate_badge_cache()
    return key
