"""Add pending ledger invitations

Revision ID: 20260705_0004
Revises: 20260628_0003
"""
from alembic import op
import sqlalchemy as sa

revision = "20260705_0004"
down_revision = "20260628_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ledger_members", sa.Column("status", sa.String(20), nullable=False, server_default="active"))
    op.add_column("ledger_members", sa.Column("invited_by", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_ledger_members_invited_by_users",
        "ledger_members", "users", ["invited_by"], ["id"], ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint("fk_ledger_members_invited_by_users", "ledger_members", type_="foreignkey")
    op.drop_column("ledger_members", "invited_by")
    op.drop_column("ledger_members", "status")
