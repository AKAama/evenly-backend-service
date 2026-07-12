import uuid
from datetime import datetime, date
from sqlalchemy import (
    CheckConstraint,
    Column,
    String,
    Numeric,
    Text,
    Date,
    DateTime,
    ForeignKey,
    Enum as SQLEnum,
    UniqueConstraint,
)
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
    __table_args__ = (
        CheckConstraint("total_amount > 0", name="ck_expenses_total_amount_positive"),
        CheckConstraint("refund_amount >= 0", name="ck_expenses_refund_amount_non_negative"),
        CheckConstraint("refund_amount < total_amount", name="ck_expenses_refund_lt_total"),
        CheckConstraint(
            "(icon_type IS NULL AND icon_value IS NULL) OR "
            "(icon_type IN ('sf_symbol', 'emoji') AND icon_value IS NOT NULL)",
            name="ck_expenses_icon_pair",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ledger_id = Column(UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="CASCADE"), nullable=False)
    payer_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    title = Column(String(255))
    total_amount = Column(Numeric(12, 2), nullable=False)
    # Partial refund from merchant/etc. Effective spend = total_amount - refund_amount.
    refund_amount = Column(Numeric(12, 2), nullable=False, default=0, server_default="0")
    note = Column(Text)
    category = Column(String(50), nullable=True)
    icon_type = Column(String(20), nullable=True)
    icon_value = Column(String(100), nullable=True)
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
    __table_args__ = (
        UniqueConstraint("expense_id", "member_id", name="uq_expense_splits_expense_member"),
        CheckConstraint("amount > 0", name="ck_expense_splits_amount_positive"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    expense_id = Column(UUID(as_uuid=True), ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    member_id = Column(UUID(as_uuid=True), ForeignKey("ledger_members.id"), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    expense = relationship("Expense", back_populates="splits")
    user = relationship("User", back_populates="expense_splits")


class ExpenseConfirmation(Base):
    __tablename__ = "expense_confirmations"
    __table_args__ = (
        UniqueConstraint("expense_id", "user_id", name="uq_expense_confirmations_expense_user"),
        CheckConstraint("status in ('confirmed', 'rejected')", name="ck_expense_confirmations_status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    expense_id = Column(UUID(as_uuid=True), ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(20), nullable=False)  # 'confirmed' or 'rejected'
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    expense = relationship("Expense", back_populates="confirmations")
    user = relationship("User", back_populates="expense_confirmations")
