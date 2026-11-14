"""Portable summaries and provider-native replay state."""
from alembic import op
import sqlalchemy as sa
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("context", sa.JSON(), nullable=True))
    op.add_column("conversations", sa.Column("native_context", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("conversations", "native_context")
    op.drop_column("conversations", "context")
