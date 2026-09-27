"""add audited submission repair plan

Revision ID: f6c7d8e9a0b1
Revises: e8f4c2d176a9
Create Date: 2026-07-31 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "f6c7d8e9a0b1"
down_revision = "e8f4c2d176a9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("meeting_submissions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("repair_plan", sa.JSON(), nullable=False, server_default="{}")
        )


def downgrade():
    with op.batch_alter_table("meeting_submissions", schema=None) as batch_op:
        batch_op.drop_column("repair_plan")
