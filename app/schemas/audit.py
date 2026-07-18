from datetime import datetime, timezone
from uuid import UUID
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    actor_user_id: UUID | None = None
    actor_label: str | None = None
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    ledger_id: UUID | None = None
    source: str
    summary: str | None = None
    metadata_json: dict[str, Any] | None = Field(default=None, validation_alias="metadata_json")
    ip: str | None = None

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        """Emit explicit UTC (…Z). Naive values from DB are treated as UTC."""
        if value is None:
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AuditEventListResponse(BaseModel):
    day: str
    total: int
    items: list[AuditEventResponse]


class ClientAuditEventCreate(BaseModel):
    """Optional client-side breadcrumbs (screen open, etc.)."""

    action: str = Field(min_length=1, max_length=64)
    summary: str | None = Field(default=None, max_length=500)
    resource_type: str | None = Field(default=None, max_length=40)
    resource_id: str | None = Field(default=None, max_length=64)
    ledger_id: UUID | None = None
    metadata: dict[str, Any] | None = None


class ClientAuditBatchCreate(BaseModel):
    events: list[ClientAuditEventCreate] = Field(min_length=1, max_length=50)
