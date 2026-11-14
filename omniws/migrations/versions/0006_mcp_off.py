"""MCP servers a conversation turned off (the others enabled in settings are used)."""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("mcp_off", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("conversations", "mcp_off")
