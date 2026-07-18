"""user badge (nameplate) for special friend identifiers

Revision ID: 20260718_0023
Revises: 20260717_0022
Create Date: 2026-07-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260718_0023"
down_revision: Union[str, None] = "20260717_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("badge", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "badge")
