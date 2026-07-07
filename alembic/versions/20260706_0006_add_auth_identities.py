"""Add unified authentication identities.

Revision ID: 20260706_0006
Revises: 20260705_0005
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260706_0006"
down_revision = "20260705_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "auth_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("provider_subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "provider_subject",
            name="uq_auth_identity_provider_subject",
        ),
    )
    op.create_index(
        op.f("ix_auth_identities_user_id"),
        "auth_identities",
        ["user_id"],
        unique=False,
    )
    op.execute(
        """
        INSERT INTO auth_identities (
            id, user_id, provider, provider_subject, email, password_hash,
            created_at, updated_at
        )
        SELECT
            id, id, 'password', lower(trim(email)),
            lower(trim(email)), password_hash, created_at, updated_at
        FROM users
        """
    )


def downgrade():
    op.drop_index(op.f("ix_auth_identities_user_id"), table_name="auth_identities")
    op.drop_table("auth_identities")
