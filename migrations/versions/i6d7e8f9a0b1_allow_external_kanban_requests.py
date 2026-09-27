"""allow external customer requests

Revision ID: i6d7e8f9a0b1
Revises: h5c6d7e8f9a0
Create Date: 2026-09-23 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "i6d7e8f9a0b1"
down_revision = "h5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("kanban_requests") as batch:
        batch.alter_column("source_action_id", existing_type=sa.String(36), nullable=True)
        batch.alter_column("meeting_id", existing_type=sa.String(36), nullable=True)
        batch.alter_column("analysis_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(
            sa.Column("source_kind", sa.String(20), nullable=False, server_default="meeting")
        )
        batch.add_column(sa.Column("source_channel", sa.String(30), nullable=True))
        batch.create_index("ix_kanban_requests_source_kind", ["source_kind"])


def downgrade():
    op.execute(sa.text("DELETE FROM kanban_requests WHERE source_action_id IS NULL"))
    with op.batch_alter_table("kanban_requests") as batch:
        batch.drop_index("ix_kanban_requests_source_kind")
        batch.drop_column("source_channel")
        batch.drop_column("source_kind")
        batch.alter_column("analysis_id", existing_type=sa.String(36), nullable=False)
        batch.alter_column("meeting_id", existing_type=sa.String(36), nullable=False)
        batch.alter_column("source_action_id", existing_type=sa.String(36), nullable=False)