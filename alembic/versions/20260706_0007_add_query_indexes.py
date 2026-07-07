"""Add indexes for common ledger reads.

Revision ID: 20260706_0007
Revises: 20260706_0006
"""
from alembic import op


revision = "20260706_0007"
down_revision = "20260706_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_ledger_members_user_status",
        "ledger_members",
        ["user_id", "status"],
    )
    op.create_index(
        "ix_expenses_ledger_created",
        "expenses",
        ["ledger_id", op.f("created_at")],
    )
    op.create_index(
        "ix_settlements_ledger_settled",
        "settlements",
        ["ledger_id", op.f("settled_at")],
    )
    op.create_unique_constraint(
        "uq_expense_confirmations_expense_user",
        "expense_confirmations",
        ["expense_id", "user_id"],
    )


def downgrade():
    op.drop_constraint(
        "uq_expense_confirmations_expense_user",
        "expense_confirmations",
        type_="unique",
    )
    op.drop_index("ix_settlements_ledger_settled", table_name="settlements")
    op.drop_index("ix_expenses_ledger_created", table_name="expenses")
    op.drop_index("ix_ledger_members_user_status", table_name="ledger_members")
