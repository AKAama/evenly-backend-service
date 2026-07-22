"""Soft account deactivation: keep ledger history, transfer or archive owned ledgers."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models import AuthIdentity, Ledger, LedgerMember, PushDevice, User
from app.services.auth import get_password_hash
from app.services.push import send_push_safely

logger = logging.getLogger(__name__)

USER_STATUS_ACTIVE = "active"
USER_STATUS_DEACTIVATED = "deactivated"
LEDGER_STATUS_ACTIVE = "active"
LEDGER_STATUS_ARCHIVED = "archived"
ARCHIVE_REASON_SOLE_OWNER = "sole_owner_deactivated"


@dataclass
class MemberBrief:
    user_id: UUID
    display_name: str
    username: str


@dataclass
class TransferPreviewItem:
    ledger_id: UUID
    ledger_name: str
    member_count_registered_active: int
    default_successor: MemberBrief | None
    candidates: list[MemberBrief]


@dataclass
class ArchivePreviewItem:
    ledger_id: UUID
    ledger_name: str
    action: str = "archive"
    reason: str = ARCHIVE_REASON_SOLE_OWNER


@dataclass
class DeactivationPreview:
    owned_ledgers_requiring_transfer: list[TransferPreviewItem]
    owned_ledgers_to_archive: list[ArchivePreviewItem]
    membership_ledger_count: int


@dataclass
class TransferResult:
    ledger_id: UUID
    ledger_name: str
    action: str  # transfer | archive
    new_owner: MemberBrief | None


def _user_is_active(user: User | None) -> bool:
    if user is None:
        return False
    return (getattr(user, "status", None) or USER_STATUS_ACTIVE) == USER_STATUS_ACTIVE


def qualified_successors(db: Session, ledger: Ledger, owner_id: UUID) -> list[tuple[LedgerMember, User]]:
    """Formal active members excluding owner and deactivated users, earliest join first."""
    members = (
        db.query(LedgerMember)
        .options(joinedload(LedgerMember.user))
        .filter(
            LedgerMember.ledger_id == ledger.id,
            LedgerMember.user_id.is_not(None),
            LedgerMember.user_id != owner_id,
            LedgerMember.status == "active",
        )
        .order_by(LedgerMember.created_at.asc())
        .all()
    )
    result: list[tuple[LedgerMember, User]] = []
    for m in members:
        if m.user is not None and _user_is_active(m.user):
            result.append((m, m.user))
    return result


def _brief(user: User) -> MemberBrief:
    return MemberBrief(
        user_id=user.id,
        display_name=(user.display_name or user.username or "").strip() or user.username,
        username=user.username,
    )


def build_preview(db: Session, user: User) -> DeactivationPreview:
    owned = (
        db.query(Ledger)
        .filter(Ledger.owner_id == user.id)
        .order_by(Ledger.created_at.asc())
        .all()
    )
    to_transfer: list[TransferPreviewItem] = []
    to_archive: list[ArchivePreviewItem] = []

    for ledger in owned:
        # Skip already archived
        if (getattr(ledger, "status", None) or LEDGER_STATUS_ACTIVE) == LEDGER_STATUS_ARCHIVED:
            continue
        successors = qualified_successors(db, ledger, user.id)
        if not successors:
            to_archive.append(
                ArchivePreviewItem(ledger_id=ledger.id, ledger_name=ledger.name)
            )
            continue
        candidates = [_brief(u) for _, u in successors]
        to_transfer.append(
            TransferPreviewItem(
                ledger_id=ledger.id,
                ledger_name=ledger.name,
                member_count_registered_active=len(successors) + 1,
                default_successor=candidates[0],
                candidates=candidates,
            )
        )

    membership_count = (
        db.query(LedgerMember)
        .filter(
            LedgerMember.user_id == user.id,
            LedgerMember.status == "active",
        )
        .count()
    )

    return DeactivationPreview(
        owned_ledgers_requiring_transfer=to_transfer,
        owned_ledgers_to_archive=to_archive,
        membership_ledger_count=int(membership_count),
    )


def _resolve_transfers(
    db: Session,
    user: User,
    owner_transfers: list[dict],
) -> list[tuple[Ledger, UUID | None, str]]:
    """
    Return list of (ledger, new_owner_id|None, action).
    new_owner_id is None only for archive.
    """
    preview = build_preview(db, user)
    transfer_map = {
        item["ledger_id"]: item.get("new_owner_id")
        for item in owner_transfers
        if item.get("ledger_id") is not None
    }

    plan: list[tuple[Ledger, UUID | None, str]] = []

    for item in preview.owned_ledgers_requiring_transfer:
        ledger = db.query(Ledger).filter(Ledger.id == item.ledger_id).first()
        if ledger is None:
            continue
        requested = transfer_map.get(item.ledger_id)
        if requested is None:
            if item.default_successor is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"账本「{item.ledger_name}」无法自动指定新管理员",
                )
            new_owner_id = item.default_successor.user_id
        else:
            allowed = {c.user_id for c in item.candidates}
            if requested not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"账本「{item.ledger_name}」的新管理员无效",
                )
            new_owner_id = requested
        plan.append((ledger, new_owner_id, "transfer"))

    for item in preview.owned_ledgers_to_archive:
        ledger = db.query(Ledger).filter(Ledger.id == item.ledger_id).first()
        if ledger is None:
            continue
        plan.append((ledger, None, "archive"))

    return plan


def deactivate_user(
    db: Session,
    user: User,
    *,
    owner_transfers: list[dict] | None = None,
    actor: User | None = None,
    admin: bool = False,
) -> list[TransferResult]:
    """
    Soft-deactivate user: transfer/archive owned ledgers, scrub credentials,
    keep memberships and expenses. Commits the session.
    """
    if getattr(user, "status", None) == USER_STATUS_DEACTIVATED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="账号已注销")

    if (getattr(user, "account_kind", None) or "app") == "platform" and not admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="平台账号请在控制台由管理员处理",
        )

    owner_transfers = owner_transfers or []
    # Normalize UUID keys
    normalized: list[dict] = []
    for row in owner_transfers:
        lid = row.get("ledger_id")
        nid = row.get("new_owner_id")
        if lid is not None and not isinstance(lid, UUID):
            lid = UUID(str(lid))
        if nid is not None and not isinstance(nid, UUID):
            nid = UUID(str(nid))
        normalized.append({"ledger_id": lid, "new_owner_id": nid})

    plan = _resolve_transfers(db, user, normalized)
    results: list[TransferResult] = []
    actor_name = (user.display_name or user.username or "用户").strip()

    now = datetime.utcnow()

    for ledger, new_owner_id, action in plan:
        if action == "transfer" and new_owner_id is not None:
            ledger.owner_id = new_owner_id
            ledger.updated_at = now
            new_owner = db.query(User).filter(User.id == new_owner_id).first()
            brief = _brief(new_owner) if new_owner else None
            results.append(
                TransferResult(
                    ledger_id=ledger.id,
                    ledger_name=ledger.name,
                    action="transfer",
                    new_owner=brief,
                )
            )
        else:
            ledger.status = LEDGER_STATUS_ARCHIVED
            ledger.archived_at = now
            ledger.archive_reason = ARCHIVE_REASON_SOLE_OWNER
            ledger.updated_at = now
            results.append(
                TransferResult(
                    ledger_id=ledger.id,
                    ledger_name=ledger.name,
                    action="archive",
                    new_owner=None,
                )
            )

    # Freeze display + mark deactivated
    frozen = (user.display_name or user.username or "用户").strip()
    user.display_name_frozen = frozen[:100]
    user.status = USER_STATUS_DEACTIVATED
    user.deactivated_at = now
    user.username_held_until = now + timedelta(days=int(settings.username_release_days or 90))
    user.badge = None
    user.updated_at = now

    # Release email immediately (placeholder unique email)
    avatar_url = user.avatar_url
    user.email = f"deleted+{user.id}@invalid.local"
    user.avatar_url = None
    # Scrub password
    dead_hash = get_password_hash(secrets.token_urlsafe(32))
    user.password_hash = dead_hash

    # Drop auth identities (releases Apple subject + password login email)
    db.query(AuthIdentity).filter(AuthIdentity.user_id == user.id).delete(
        synchronize_session=False
    )

    # Disable push devices
    db.query(PushDevice).filter(PushDevice.user_id == user.id).update(
        {"is_active": False},
        synchronize_session=False,
    )

    db.flush()

    from app.services.audit import record_audit

    record_audit(
        db,
        action="user.deactivate_admin" if admin else "user.deactivate",
        actor=actor or user,
        resource_type="user",
        resource_id=user.id,
        summary=(
            f"{'管理员注销' if admin else '注销账号'} {frozen} "
            f"移交{sum(1 for r in results if r.action == 'transfer')}本 "
            f"归档{sum(1 for r in results if r.action == 'archive')}本"
        ),
        metadata={
            "transfers": [
                {
                    "ledger_id": str(r.ledger_id),
                    "ledger_name": r.ledger_name,
                    "action": r.action,
                    "new_owner_id": str(r.new_owner.user_id) if r.new_owner else None,
                }
                for r in results
            ]
        },
        request=None,
    )

    db.commit()

    # Push after commit so new-owner devices are still valid
    for r in results:
        if r.action != "transfer" or r.new_owner is None:
            continue
        payload = {
            "aps": {
                "alert": {
                    "title": "你成为账本管理员",
                    "body": f"{actor_name} 已将账本「{r.ledger_name}」的管理权移交给你。",
                },
                "sound": "default",
            },
            "event": "ledger.owner_transferred",
            "ledger_id": str(r.ledger_id),
        }
        send_push_safely(db, [r.new_owner.user_id], payload)

    # Best-effort COS cleanup
    if avatar_url and settings.cos:
        try:
            from app.services.cos import get_cos_service

            cos_service = get_cos_service()
            if cos_service is not None:
                cos_service.delete_file(avatar_url)
        except Exception:
            logger.exception("Avatar cleanup failed for deactivated user_id=%s", user.id)

    logger.info(
        "Account deactivated user_id=%s transfers=%d archives=%d admin=%s",
        user.id,
        sum(1 for r in results if r.action == "transfer"),
        sum(1 for r in results if r.action == "archive"),
        admin,
    )
    return results


def is_username_held(db: Session, username: str) -> bool:
    """True if an active hold blocks this username (deactivated user within cooldown)."""
    now = datetime.utcnow()
    row = (
        db.query(User)
        .filter(
            User.username.ilike(username.strip()),
            User.status == USER_STATUS_DEACTIVATED,
            User.username_held_until.is_not(None),
            User.username_held_until > now,
        )
        .first()
    )
    return row is not None


def release_expired_usernames(db: Session, username: str | None = None) -> int:
    """Rename usernames past hold so they can be re-registered. Returns count released."""
    now = datetime.utcnow()
    q = db.query(User).filter(
        User.status == USER_STATUS_DEACTIVATED,
        User.username_held_until.is_not(None),
        User.username_held_until <= now,
        ~User.username.like("released_%"),
    )
    if username:
        q = q.filter(User.username.ilike(username.strip()))
    count = 0
    for u in q.all():
        short = str(u.id).replace("-", "")[:12]
        u.username = f"released_{short}"[:30]
        u.username_held_until = None
        count += 1
    if count:
        db.commit()
    return count


def ensure_username_available(db: Session, username: str) -> None:
    """Raise 400 if username is taken by active user or still in hold."""
    from app.services.auth import get_user_by_username

    release_expired_usernames(db, username)
    existing = get_user_by_username(db, username)
    if existing is None:
        return
    if getattr(existing, "status", None) == USER_STATUS_DEACTIVATED:
        held_until = getattr(existing, "username_held_until", None)
        if held_until is not None and held_until > datetime.utcnow():
            raise HTTPException(status_code=400, detail="用户名不可用")
        # Past hold but still same username string — release and allow
        if not str(existing.username).lower().startswith("released_"):
            release_expired_usernames(db, username)
            existing = get_user_by_username(db, username)
            if existing is None:
                return
        if str(existing.username).lower().startswith("released_"):
            return
        raise HTTPException(status_code=400, detail="用户名不可用")
    raise HTTPException(status_code=400, detail="用户名已被使用")
