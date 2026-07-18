"""badge definitions table (admin-managed nameplates)

Revision ID: 20260718_0024
Revises: 20260718_0023
Create Date: 2026-07-18

Moves fixed badge catalog into a DB table so platform admins can
create/edit/delete 创始人/船员/搭子 etc.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260718_0024"
down_revision: Union[str, None] = "20260718_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Seed matches previous hardcoded catalog
_SEED = [
    ("founder", "创始人", "Evenly 主理人", "gold", 10),
    ("crew", "船员", "一起造 Evenly 的伙伴", "blue", 20),
    ("mate", "搭子", "亲密朋友、常一起分账", "orange", 30),
    ("beta", "内测官", "最早一批使用者", "purple", 40),
    ("vip", "特邀", "特别嘉宾", "magenta", 50),
]


def upgrade() -> None:
    op.create_table(
        "badges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=40), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column("color", sa.String(length=32), nullable=False, server_default="blue"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("key", name="uq_badges_key"),
    )
    op.create_index("ix_badges_key", "badges", ["key"])

    badges = sa.table(
        "badges",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("key", sa.String),
        sa.column("label", sa.String),
        sa.column("description", sa.String),
        sa.column("color", sa.String),
        sa.column("sort_order", sa.Integer),
        sa.column("is_active", sa.Boolean),
    )
    import uuid

    op.bulk_insert(
        badges,
        [
            {
                "id": uuid.uuid4(),
                "key": key,
                "label": label,
                "description": desc,
                "color": color,
                "sort_order": order,
                "is_active": True,
            }
            for key, label, desc, color, order in _SEED
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_badges_key", table_name="badges")
    op.drop_table("badges")
