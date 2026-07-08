"""Simplify ledger member storage.

Revision ID: 20260707_0009
Revises: 20260706_0008
"""
from alembic import op
import sqlalchemy as sa


revision = "20260707_0009"
down_revision = "20260706_0008"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "uq_ledger_members_ledger_temporary_name",
        "ledger_members",
        type_="unique",
    )
    op.alter_column("ledger_members", "nickname", new_column_name="display_name")
    op.execute(
        """
        UPDATE ledger_members
        SET display_name = COALESCE(display_name, temporary_name, 'Unknown')
        """
    )
    op.alter_column("ledger_members", "display_name", nullable=False)
    op.alter_column("ledger_members", "joined_at", new_column_name="created_at")
    op.create_index(
        "uq_ledger_members_ledger_temp_name",
        "ledger_members",
        ["ledger_id", "display_name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.drop_constraint(
        "fk_ledger_members_invited_by_users",
        "ledger_members",
        type_="foreignkey",
    )
    op.drop_column("ledger_members", "is_temporary")
    op.drop_column("ledger_members", "temporary_name")
    op.drop_column("ledger_members", "invited_by")


def downgrade():
    op.add_column(
        "ledger_members",
        sa.Column("invited_by", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ledger_members_invited_by_users",
        "ledger_members",
        "users",
        ["invited_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "ledger_members",
        sa.Column("temporary_name", sa.String(100), nullable=True),
    )
    op.add_column(
        "ledger_members",
        sa.Column("is_temporary", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.execute(
        "UPDATE ledger_members SET temporary_name = display_name WHERE user_id IS NULL"
    )
    op.drop_index("uq_ledger_members_ledger_temp_name", table_name="ledger_members")
    op.alter_column("ledger_members", "created_at", new_column_name="joined_at")
    op.alter_column("ledger_members", "display_name", new_column_name="nickname", nullable=True)
    op.create_unique_constraint(
        "uq_ledger_members_ledger_temporary_name",
        "ledger_members",
        ["ledger_id", "temporary_name"],
    )
