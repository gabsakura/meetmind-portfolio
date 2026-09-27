"""add per-user sensitive request limit

Revision ID: 2f49a6d091b8
Revises: 8be0965aa073
Create Date: 2026-07-24 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "2f49a6d091b8"
down_revision = "8be0965aa073"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "request_limit_per_10_minutes",
                sa.Integer(),
                nullable=False,
                server_default="5",
            )
        )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column(
            "request_limit_per_10_minutes",
            server_default=None,
        )


def downgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("request_limit_per_10_minutes")
