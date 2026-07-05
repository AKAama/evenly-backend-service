"""Add unique login username

Revision ID: 20260705_0005
Revises: 20260705_0004
"""
from alembic import op

revision = "20260705_0005"
down_revision = "20260705_0004"
branch_labels = None
depends_on = None


def upgrade():
    # Production may already have this column from a manual hotfix. Keep the
    # migration safe to rerun so Alembic can still record the revision.
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(30)")
    op.execute(
        """
        UPDATE users
        SET username = 'user_' || left(replace(id::text, '-', ''), 12)
        WHERE username IS NULL
        """
    )
    op.execute("ALTER TABLE users ALTER COLUMN username SET NOT NULL")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_lower
        ON users (lower(username))
        """
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_users_username_lower")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS username")
