"""Fix ledger member temporary-member schema drift

Revision ID: 20260624_0002
Revises: 20260614_0001
Create Date: 2026-06-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260624_0002"
down_revision: Union[str, None] = "20260614_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    op.alter_column(
        "ledger_members",
        "id",
        existing_type=sa.UUID(),
        server_default=sa.text("gen_random_uuid()"),
        existing_nullable=False,
    )
    op.alter_column(
        "ledger_members",
        "user_id",
        existing_type=sa.UUID(),
        nullable=True,
    )
    op.alter_column(
        "ledger_members",
        "is_temporary",
        existing_type=sa.Boolean(),
        server_default=sa.text("false"),
        existing_nullable=True,
    )
    op.create_unique_constraint(
        "uq_ledger_members_ledger_user",
        "ledger_members",
        ["ledger_id", "user_id"],
    )
    op.create_unique_constraint(
        "uq_ledger_members_ledger_temporary_name",
        "ledger_members",
        ["ledger_id", "temporary_name"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_ledger_members_ledger_temporary_name", "ledger_members", type_="unique")
    op.drop_constraint("uq_ledger_members_ledger_user", "ledger_members", type_="unique")
    op.alter_column(
        "ledger_members",
        "is_temporary",
        existing_type=sa.Boolean(),
        server_default=None,
        existing_nullable=True,
    )
    op.alter_column(
        "ledger_members",
        "user_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
    op.alter_column(
        "ledger_members",
        "id",
        existing_type=sa.UUID(),
        server_default=None,
        existing_nullable=False,
    )
