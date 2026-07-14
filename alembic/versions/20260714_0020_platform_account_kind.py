"""Add users.account_kind (app | platform) for ops-only accounts.

Revision ID: 20260714_0020
Revises: 20260714_0019
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260714_0020"
down_revision = "20260714_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "account_kind",
            sa.String(length=20),
            nullable=False,
            server_default="app",
        ),
    )
    op.create_check_constraint(
        "ck_users_account_kind",
        "users",
        "account_kind IN ('app', 'platform')",
    )
    # Existing admins (if any) become platform operators.
    op.execute("UPDATE users SET account_kind = 'platform' WHERE is_admin = true")


def downgrade() -> None:
    op.drop_constraint("ck_users_account_kind", "users", type_="check")
    op.drop_column("users", "account_kind")
