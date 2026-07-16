"""ledger require_confirmation setting

Revision ID: 20260716_0021
Revises: 20260714_0020
Create Date: 2026-07-16

Per-ledger toggle: when true, expenses need participant confirmation before
settlement/share; when false, new expenses are auto-confirmed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260716_0021"
down_revision: Union[str, None] = "20260714_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ledgers",
        sa.Column(
            "require_confirmation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ledgers", "require_confirmation")
