import uuid
from datetime import datetime
from sqlalchemy import Column, String, Numeric, Text, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class Settlement(Base):
    __tablename__ = "settlements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ledger_id = Column(UUID(as_uuid=True), ForeignKey("ledgers.id", ondelete="CASCADE"), nullable=False)
    from_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    to_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    note = Column(Text)
    settled_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    ledger = relationship("Ledger", back_populates="settlements")
    from_user = relationship("User", back_populates="settlements_from", foreign_keys=[from_user_id])
    to_user = relationship("User", back_populates="settlements_to", foreign_keys=[to_user_id])
