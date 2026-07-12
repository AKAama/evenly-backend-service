"""Remove expense kind (income) and group_id compound linking.

Deletes any income rows first, then drops the columns introduced in 0014/0015.

Revision ID: 20260712_0016
Revises: 20260712_0015
Create Date: 2026-07-12
"""

from alembic import op
import sqlalchemy as sa


revision = "20260712_0016"
down_revision = "20260712_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Income rows and their splits/confirmations (cascade) go away with the feature.
    op.execute("DELETE FROM expenses WHERE kind = 'income'")
    op.drop_index("ix_expenses_group_id", table_name="expenses")
    op.drop_column("expenses", "group_id")
    op.drop_constraint("ck_expenses_kind", "expenses", type_="check")
    op.drop_column("expenses", "kind")


def downgrade() -> None:
    op.add_column(
        "expenses",
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="expense"),
    )
    op.create_check_constraint(
        "ck_expenses_kind",
        "expenses",
        "kind IN ('expense', 'income')",
    )
    op.add_column("expenses", sa.Column("group_id", sa.UUID(), nullable=True))
    op.create_index("ix_expenses_group_id", "expenses", ["group_id"])
