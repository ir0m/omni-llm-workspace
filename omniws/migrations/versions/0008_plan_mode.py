"""Plan mode per conversation (Claude Code in Code): explore and propose, edit only after the plan is approved."""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("plan_mode", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("conversations", "plan_mode")
