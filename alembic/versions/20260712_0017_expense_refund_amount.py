"""Add expense refund_amount (partial refunds reduce effective spend).

Revision ID: 20260712_0017
Revises: 20260712_0016
Create Date: 2026-07-12
"""

from alembic import op
import sqlalchemy as sa


revision = "20260712_0017"
down_revision = "20260712_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "expenses",
        sa.Column(
            "refund_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_expenses_refund_amount_non_negative",
        "expenses",
        "refund_amount >= 0",
    )
    op.create_check_constraint(
        "ck_expenses_refund_lt_total",
        "expenses",
        "refund_amount < total_amount",
    )


def downgrade() -> None:
    op.drop_constraint("ck_expenses_refund_lt_total", "expenses", type_="check")
    op.drop_constraint("ck_expenses_refund_amount_non_negative", "expenses", type_="check")
    op.drop_column("expenses", "refund_amount")
