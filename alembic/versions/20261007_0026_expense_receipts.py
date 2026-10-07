"""Store up to three image receipts on expenses."""
from alembic import op
import sqlalchemy as sa

revision = "20261007_0026"
down_revision = "20260722_0025"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("expenses", sa.Column("receipt_urls", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    op.drop_column("expenses", "receipt_urls")
