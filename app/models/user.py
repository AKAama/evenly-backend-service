import uuid
from datetime import datetime
from sqlalchemy import Boolean, Column, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    username = Column(String(30), nullable=False, index=True)
    username_is_generated = Column(Boolean, nullable=False, default=False)
    password_hash = Column(String, nullable=False)
    display_name = Column(String(100))
    avatar_url = Column(String)
    # Optional nameplate key (founder/crew/mate/beta/vip); display-only, set by platform admin.
    badge = Column(String(32), nullable=True)
    # app = normal Evenly user; platform = ops-only console account (no ledger membership).
    account_kind = Column(String(20), nullable=False, default="app", server_default="app")
    # Legacy flag kept for DB compatibility; console admin is decided by account_kind=platform.
    is_admin = Column(Boolean, nullable=False, default=False, server_default="false")
    # active | deactivated — soft deactivation keeps ledger history.
    status = Column(String(20), nullable=False, default="active", server_default="active")
    deactivated_at = Column(DateTime, nullable=True)
    # Frozen at deactivation for "张三（已注销）" display.
    display_name_frozen = Column(String(100), nullable=True)
    # Username may be re-registered after this time (email released immediately).
    username_held_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def is_platform(self) -> bool:
        return (self.account_kind or "app") == "platform"

    @property
    def is_deactivated(self) -> bool:
        return (self.status or "active") == "deactivated"

    @property
    def public_display_name(self) -> str:
        """Client-facing label; appends （已注销） when deactivated."""
        if self.is_deactivated:
            base = (self.display_name_frozen or self.display_name or self.username or "用户").strip()
            if base.endswith("（已注销）"):
                return base
            return f"{base}（已注销）"
        return (self.display_name or self.username or self.email or "用户").strip()

    # Relationships
    ledgers = relationship("Ledger", back_populates="owner", cascade="all, delete-orphan")
    memberships = relationship(
        "LedgerMember",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="LedgerMember.user_id",
    )
    expenses_created = relationship("Expense", back_populates="creator", foreign_keys="Expense.created_by")
    expenses_paid = relationship("Expense", back_populates="payer", foreign_keys="Expense.payer_id")
    settlements_from = relationship("Settlement", back_populates="from_user", foreign_keys="Settlement.from_user_id")
    settlements_to = relationship("Settlement", back_populates="to_user", foreign_keys="Settlement.to_user_id")
    expense_splits = relationship("ExpenseSplit", back_populates="user")
    expense_confirmations = relationship("ExpenseConfirmation", back_populates="user")
    auth_identities = relationship(
        "AuthIdentity",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    push_devices = relationship("PushDevice", back_populates="user", cascade="all, delete-orphan")


class PushDevice(Base):
    __tablename__ = "push_devices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(200), unique=True, nullable=False, index=True)
    environment = Column(String(20), nullable=False)
    bundle_id = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="push_devices")


class AuthIdentity(Base):
    __tablename__ = "auth_identities"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_subject",
            name="uq_auth_identity_provider_subject",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(String(20), nullable=False)
    provider_subject = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True)
    phone = Column(String(32), nullable=True)
    password_hash = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="auth_identities")
