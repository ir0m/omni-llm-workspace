"""Keep queued instructions when the browser disconnects or the server stops."""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("pending_inputs", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("conversations", "pending_inputs")
