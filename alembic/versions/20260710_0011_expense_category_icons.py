"""Add synchronized category and icon fields to expenses.

Revision ID: 20260710_0011
Revises: 20260710_0010
"""

from alembic import op
import sqlalchemy as sa


revision = "20260710_0011"
down_revision = "20260710_0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("expenses", sa.Column("category", sa.String(length=50), nullable=True))
    op.add_column("expenses", sa.Column("icon_type", sa.String(length=20), nullable=True))
    op.add_column("expenses", sa.Column("icon_value", sa.String(length=100), nullable=True))
    op.create_check_constraint(
        "ck_expenses_icon_pair",
        "expenses",
        "(icon_type IS NULL AND icon_value IS NULL) OR "
        "(icon_type IN ('sf_symbol', 'emoji') AND icon_value IS NOT NULL)",
    )


def downgrade():
    op.drop_constraint("ck_expenses_icon_pair", "expenses", type_="check")
    op.drop_column("expenses", "icon_value")
    op.drop_column("expenses", "icon_type")
    op.drop_column("expenses", "category")
