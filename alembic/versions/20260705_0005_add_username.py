"""Add unique login username

Revision ID: 20260705_0005
Revises: 20260705_0004
"""
from alembic import op
import sqlalchemy as sa

revision = "20260705_0005"
down_revision = "20260705_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("username", sa.String(30), nullable=True))
    op.execute("UPDATE users SET username = 'user_' || left(replace(id::text, '-', ''), 12) WHERE username IS NULL")
    op.alter_column("users", "username", nullable=False)
    op.create_index("uq_users_username_lower", "users", [sa.text("lower(username)")], unique=True)


def downgrade():
    op.drop_index("uq_users_username_lower", table_name="users")
    op.drop_column("users", "username")
