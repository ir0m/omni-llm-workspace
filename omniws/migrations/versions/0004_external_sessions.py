"""Index CLI sessions and remember imported history without duplicating turns."""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("native_origin", sa.JSON(), nullable=True))
    op.create_table(
        "external_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("family", sa.String(40), nullable=False),
        sa.Column("agent", sa.String(40), nullable=False),
        sa.Column("native_id", sa.String(200), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="SET NULL")),
        sa.Column("project", sa.String(200), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("turns", sa.JSON(), nullable=False),
        sa.Column("pending", sa.JSON(), nullable=False),
        sa.UniqueConstraint("family", "native_id"),
    )
    op.create_index("ix_external_sessions_conversation_id", "external_sessions", ["conversation_id"])


def downgrade() -> None:
    op.drop_table("external_sessions")
    op.drop_column("messages", "native_origin")
