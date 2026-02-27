import uuid
from datetime import datetime, date
from sqlalchemy import Column, String, Numeric, Text, Date, DateTime, ForeignKey, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum

from app.database import Base


class ExpenseStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Expense(Base):
    __tablename__ = "expenses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ledger_id = Column(UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="CASCADE"), nullable=False)
    payer_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    title = Column(String(255))
    total_amount = Column(Numeric(12, 2), nullable=False)
    note = Column(Text)
    expense_date = Column(Date, nullable=False)
    status = Column(SQLEnum(ExpenseStatus), default=ExpenseStatus.PENDING, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    ledger = relationship("Ledger", back_populates="expenses")
    payer = relationship("User", back_populates="expenses_paid", foreign_keys=[payer_id])
    creator = relationship("User", back_populates="expenses_created", foreign_keys=[created_by])
    splits = relationship("ExpenseSplit", back_populates="expense", cascade="all, delete-orphan")
    confirmations = relationship("ExpenseConfirmation", back_populates="expense", cascade="all, delete-orphan")


class ExpenseSplit(Base):
    __tablename__ = "expense_splits"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    expense_id = Column(UUID(as_uuid=True), ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    expense = relationship("Expense", back_populates="splits")
    user = relationship("User", back_populates="expense_splits")


class ExpenseConfirmation(Base):
    __tablename__ = "expense_confirmations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    expense_id = Column(UUID(as_uuid=True), ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(20), nullable=False)  # 'confirmed' or 'rejected'
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    expense = relationship("Expense", back_populates="confirmations")
    user = relationship("User", back_populates="expense_confirmations")
