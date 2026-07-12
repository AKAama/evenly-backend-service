"""Add shareable ledger invite links for QR / Universal Links.

Revision ID: 20260712_0013
Revises: 20260711_0012
"""

from alembic import op
import sqlalchemy as sa


revision = "20260712_0013"
down_revision = "20260711_0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ledger_invite_links",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ledger_id", sa.UUID(), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ledger_id"], ["ledgers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_ledger_invite_links_token"),
    )
    op.create_index("ix_ledger_invite_links_ledger_id", "ledger_invite_links", ["ledger_id"])
    op.create_index(
        "ix_ledger_invite_links_active_ledger",
        "ledger_invite_links",
        ["ledger_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )


def downgrade():
    op.drop_index("ix_ledger_invite_links_active_ledger", table_name="ledger_invite_links")
    op.drop_index("ix_ledger_invite_links_ledger_id", table_name="ledger_invite_links")
    op.drop_table("ledger_invite_links")
