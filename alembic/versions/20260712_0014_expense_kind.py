"""Add expense kind (expense | income) for winnings / refunds.

Revision ID: 20260712_0014
Revises: 20260712_0013
"""

from alembic import op
import sqlalchemy as sa


revision = "20260712_0014"
down_revision = "20260712_0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "expenses",
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="expense"),
    )
    op.create_check_constraint(
        "ck_expenses_kind",
        "expenses",
        "kind IN ('expense', 'income')",
    )


def downgrade():
    op.drop_constraint("ck_expenses_kind", "expenses", type_="check")
    op.drop_column("expenses", "kind")
