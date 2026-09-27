"""add profile photo columns to users

Revision ID: b2c9f0d1e3a4
Revises: a1b2c3d4e5f6
Create Date: 2026-08-31 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "b2c9f0d1e3a4"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("avatar_data", sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column("avatar_mime", sa.String(length=40), nullable=True))
        batch_op.add_column(
            sa.Column("avatar_updated_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("avatar_updated_at")
        batch_op.drop_column("avatar_mime")
        batch_op.drop_column("avatar_data")
