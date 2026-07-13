"""Cascade expense splits when deleting ledger members.

Revision ID: 20260713_0018
Revises: 20260712_0017
Create Date: 2026-07-13
"""

from alembic import op


revision = "20260713_0018"
down_revision = "20260712_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_expense_splits_member_id",
        "expense_splits",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_expense_splits_member_id",
        "expense_splits",
        "ledger_members",
        ["member_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_expense_splits_member_id",
        "expense_splits",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_expense_splits_member_id",
        "expense_splits",
        "ledger_members",
        ["member_id"],
        ["id"],
    )
