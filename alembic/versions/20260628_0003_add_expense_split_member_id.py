"""Allow expense splits for temporary ledger members

Revision ID: 20260628_0003
Revises: 20260624_0002
Create Date: 2026-06-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260628_0003"
down_revision: Union[str, None] = "20260624_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("expense_splits", sa.Column("member_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_expense_splits_member_id",
        "expense_splits",
        "ledger_members",
        ["member_id"],
        ["id"],
    )
    op.execute(
        """
        UPDATE expense_splits AS split
        SET member_id = member.id
        FROM expenses AS expense, ledger_members AS member
        WHERE split.expense_id = expense.id
          AND member.ledger_id = expense.ledger_id
          AND member.user_id = split.user_id
        """
    )
    op.alter_column("expense_splits", "member_id", existing_type=sa.UUID(), nullable=False)
    op.alter_column("expense_splits", "user_id", existing_type=sa.UUID(), nullable=True)
    op.execute(
        "ALTER TABLE expense_splits "
        "DROP CONSTRAINT IF EXISTS uq_expense_splits_expense_user"
    )
    op.create_unique_constraint(
        "uq_expense_splits_expense_member",
        "expense_splits",
        ["expense_id", "member_id"],
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE expense_splits "
        "DROP CONSTRAINT IF EXISTS uq_expense_splits_expense_member"
    )
    op.create_unique_constraint(
        "uq_expense_splits_expense_user",
        "expense_splits",
        ["expense_id", "user_id"],
    )
    op.alter_column("expense_splits", "user_id", existing_type=sa.UUID(), nullable=False)
    op.drop_constraint("fk_expense_splits_member_id", "expense_splits", type_="foreignkey")
    op.drop_column("expense_splits", "member_id")
