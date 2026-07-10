"""Read registered ledger member names from users.

Revision ID: 20260710_0010
Revises: 20260707_0009
"""

from alembic import op
import sqlalchemy as sa


revision = "20260710_0010"
down_revision = "20260707_0009"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("uq_ledger_members_ledger_temp_name", table_name="ledger_members")
    op.alter_column(
        "ledger_members",
        "display_name",
        new_column_name="temporary_name",
        existing_type=sa.String(length=100),
        nullable=True,
    )
    op.execute(
        "UPDATE ledger_members SET temporary_name = NULL WHERE user_id IS NOT NULL"
    )
    op.create_index(
        "uq_ledger_members_ledger_temp_name",
        "ledger_members",
        ["ledger_id", "temporary_name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.create_check_constraint(
        "ck_ledger_members_registered_or_temporary_name",
        "ledger_members",
        "(user_id IS NOT NULL AND temporary_name IS NULL) OR "
        "(user_id IS NULL AND temporary_name IS NOT NULL AND btrim(temporary_name) <> '')",
    )


def downgrade():
    op.drop_constraint(
        "ck_ledger_members_registered_or_temporary_name",
        "ledger_members",
        type_="check",
    )
    op.drop_index("uq_ledger_members_ledger_temp_name", table_name="ledger_members")
    op.execute(
        """
        UPDATE ledger_members AS lm
        SET temporary_name = COALESCE(u.display_name, u.email)
        FROM users AS u
        WHERE lm.user_id = u.id
        """
    )
    op.alter_column(
        "ledger_members",
        "temporary_name",
        new_column_name="display_name",
        existing_type=sa.String(length=100),
        nullable=False,
    )
    op.create_index(
        "uq_ledger_members_ledger_temp_name",
        "ledger_members",
        ["ledger_id", "display_name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
