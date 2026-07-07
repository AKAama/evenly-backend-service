"""Track generated usernames.

Revision ID: 20260706_0008
Revises: 20260706_0007
"""
from alembic import op
import sqlalchemy as sa


revision = "20260706_0008"
down_revision = "20260706_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "username_is_generated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        """
        UPDATE users
        SET username_is_generated = true
        WHERE username LIKE 'apple_%'
          AND EXISTS (
              SELECT 1 FROM auth_identities
              WHERE auth_identities.user_id = users.id
                AND auth_identities.provider = 'apple'
          )
          AND NOT EXISTS (
              SELECT 1 FROM auth_identities
              WHERE auth_identities.user_id = users.id
                AND auth_identities.provider = 'password'
          )
        """
    )
    op.alter_column("users", "username_is_generated", server_default=None)


def downgrade():
    op.drop_column("users", "username_is_generated")
