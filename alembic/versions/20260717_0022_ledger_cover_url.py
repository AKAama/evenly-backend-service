"""ledger cover_url for bookshelf covers

Revision ID: 20260717_0022
Revises: 20260716_0021
Create Date: 2026-07-17

Stores optional COS URL for a custom ledger book cover (same storage path
pattern as user avatars, folder ledger-covers).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260717_0022"
down_revision: Union[str, None] = "20260716_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ledgers",
        sa.Column("cover_url", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ledgers", "cover_url")
