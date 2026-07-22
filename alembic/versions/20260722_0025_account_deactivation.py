"""account deactivation + ledger archive

Revision ID: 20260722_0025
Revises: 20260718_0024
Create Date: 2026-07-22

Soft-deactivate users (keep expense history) and archive sole-owner ledgers.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0025"
down_revision: Union[str, None] = "20260718_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
    )
    op.add_column("users", sa.Column("deactivated_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("display_name_frozen", sa.String(length=100), nullable=True))
    op.add_column("users", sa.Column("username_held_until", sa.DateTime(), nullable=True))
    op.create_index("ix_users_status", "users", ["status"])

    op.add_column(
        "ledgers",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
    )
    op.add_column("ledgers", sa.Column("archived_at", sa.DateTime(), nullable=True))
    op.add_column("ledgers", sa.Column("archive_reason", sa.String(length=64), nullable=True))
    op.create_index("ix_ledgers_status", "ledgers", ["status"])


def downgrade() -> None:
    op.drop_index("ix_ledgers_status", table_name="ledgers")
    op.drop_column("ledgers", "archive_reason")
    op.drop_column("ledgers", "archived_at")
    op.drop_column("ledgers", "status")

    op.drop_index("ix_users_status", table_name="users")
    op.drop_column("users", "username_held_until")
    op.drop_column("users", "display_name_frozen")
    op.drop_column("users", "deactivated_at")
    op.drop_column("users", "status")
