import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, ForeignKey, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class Ledger(Base):
    __tablename__ = "ledgers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    currency = Column(String(10), default="CNY")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    owner = relationship("User", back_populates="ledgers")
    members = relationship("LedgerMember", back_populates="ledger", cascade="all, delete-orphan")
    expenses = relationship("Expense", back_populates="ledger", cascade="all, delete-orphan")
    settlements = relationship("Settlement", back_populates="ledger", cascade="all, delete-orphan")


class LedgerMember(Base):
    __tablename__ = "ledger_members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ledger_id = Column(UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True)  # nullable for temporary members
    nickname = Column(String(100))
    joined_at = Column(DateTime, default=datetime.utcnow)
    
    # Temporary member support
    is_temporary = Column(Boolean, default=False)
    temporary_name = Column(String(100), nullable=True)

    # Relationships
    ledger = relationship("Ledger", back_populates="members")
    user = relationship("User", back_populates="memberships")
