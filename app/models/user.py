import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    display_name = Column(String(100))
    avatar_url = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    ledgers = relationship("Ledger", back_populates="owner", cascade="all, delete-orphan")
    memberships = relationship("LedgerMember", back_populates="user", cascade="all, delete-orphan")
    expenses_created = relationship("Expense", back_populates="creator", foreign_keys="Expense.created_by")
    expenses_paid = relationship("Expense", back_populates="payer", foreign_keys="Expense.payer_id")
    settlements_from = relationship("Settlement", back_populates="from_user", foreign_keys="Settlement.from_user_id")
    settlements_to = relationship("Settlement", back_populates="to_user", foreign_keys="Settlement.to_user_id")
    expense_splits = relationship("ExpenseSplit", back_populates="user")
    expense_confirmations = relationship("ExpenseConfirmation", back_populates="user")
