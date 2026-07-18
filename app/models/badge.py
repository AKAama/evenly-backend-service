"""Admin-managed nameplate definitions (铭牌类型)."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class Badge(Base):
    __tablename__ = "badges"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Stable machine key used on users.badge (e.g. founder, crew, custom-abc)
    key = Column(String(32), unique=True, nullable=False, index=True)
    label = Column(String(40), nullable=False)
    description = Column(String(200), nullable=True)
    # Ant Design Tag color name or hex (#RRGGBB) for clients
    color = Column(String(32), nullable=False, default="blue")
    sort_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
