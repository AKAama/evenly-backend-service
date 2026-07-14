import uuid
from datetime import datetime

from sqlalchemy import Column, String, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class AuditEvent(Base):
    """Append-only activity log for admin review (login, ledger/expense ops, etc.)."""

    __tablename__ = "audit_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    actor_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_label = Column(String(120), nullable=True)
    action = Column(String(64), nullable=False, index=True)
    resource_type = Column(String(40), nullable=True)
    resource_id = Column(String(64), nullable=True)
    ledger_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    source = Column(String(20), nullable=False, default="api", server_default="api")
    summary = Column(String(500), nullable=True)
    # "metadata" is reserved on Declarative API; store as metadata_json.
    metadata_json = Column(JSON, nullable=True)
    ip = Column(String(64), nullable=True)

    actor = relationship("User", foreign_keys=[actor_user_id])
