"""Link cost+income (or multi-part) expenses via group_id.

Revision ID: 20260712_0015
Revises: 20260712_0014
"""

from alembic import op
import sqlalchemy as sa


revision = "20260712_0015"
down_revision = "20260712_0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("expenses", sa.Column("group_id", sa.UUID(), nullable=True))
    op.create_index("ix_expenses_group_id", "expenses", ["group_id"])


def downgrade():
    op.drop_index("ix_expenses_group_id", table_name="expenses")
    op.drop_column("expenses", "group_id")
