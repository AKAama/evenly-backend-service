import uuid
from datetime import datetime
from sqlalchemy import Boolean, CheckConstraint, Column, String, DateTime, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class Ledger(Base):
    __tablename__ = "ledgers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    currency = Column(String(10), default="CNY")
    # When True, expenses stay pending until required participants confirm.
    # When False, expenses are auto-confirmed and count toward settlement immediately.
    require_confirmation = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    owner = relationship("User", back_populates="ledgers")
    members = relationship("LedgerMember", back_populates="ledger", cascade="all, delete-orphan")
    expenses = relationship("Expense", back_populates="ledger", cascade="all, delete-orphan")
    settlements = relationship("Settlement", back_populates="ledger", cascade="all, delete-orphan")
    invite_links = relationship(
        "LedgerInviteLink",
        back_populates="ledger",
        cascade="all, delete-orphan",
    )


class LedgerInviteLink(Base):
    """Shareable QR / Universal Link token for joining a ledger."""

    __tablename__ = "ledger_invite_links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ledger_id = Column(UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="CASCADE"), nullable=False)
    token = Column(String(64), nullable=False, unique=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    revoked_at = Column(DateTime, nullable=True)

    ledger = relationship("Ledger", back_populates="invite_links")
    creator = relationship("User")

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class LedgerMember(Base):
    __tablename__ = "ledger_members"
    __table_args__ = (
        UniqueConstraint("ledger_id", "user_id", name="uq_ledger_members_ledger_user"),
        Index(
            "uq_ledger_members_ledger_temp_name",
            "ledger_id",
            "temporary_name",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
            sqlite_where=text("user_id IS NULL"),
        ),
        CheckConstraint(
            "(user_id IS NOT NULL AND temporary_name IS NULL) OR "
            "(user_id IS NULL AND temporary_name IS NOT NULL AND length(trim(temporary_name)) > 0)",
            name="ck_ledger_members_registered_or_temporary_name",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ledger_id = Column(UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True)  # nullable for temporary members
    temporary_name = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(20), nullable=False, default="active")

    # Relationships
    ledger = relationship("Ledger", back_populates="members")
    user = relationship("User", back_populates="memberships", foreign_keys=[user_id])

    # Transitional API aliases. These are derived and are no longer columns.
    @property
    def nickname(self):
        return self.display_name

    @nickname.setter
    def nickname(self, value):
        self.display_name = value

    @property
    def display_name(self):
        if self.user_id is not None:
            return (self.user.display_name or self.user.email) if self.user is not None else None
        return self.temporary_name

    @display_name.setter
    def display_name(self, value):
        if self.user_id is None:
            self.temporary_name = value

    @property
    def joined_at(self):
        return self.created_at

    @property
    def is_temporary(self):
        return self.user_id is None

    @is_temporary.setter
    def is_temporary(self, value):
        if value is False and self.user_id is None:
            return
