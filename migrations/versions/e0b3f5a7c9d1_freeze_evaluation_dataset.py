"""freeze evaluation dataset content

Revision ID: e0b3f5a7c9d1
Revises: d9a2e4f6b8c0
Create Date: 2026-08-07 23:15:00
"""

import sqlalchemy as sa
from alembic import op

revision = "e0b3f5a7c9d1"
down_revision = "d9a2e4f6b8c0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("evaluation_dataset_releases") as batch_op:
        batch_op.add_column(
            sa.Column("dataset_jsonl", sa.Text(), nullable=False, server_default="")
        )


def downgrade():
    with op.batch_alter_table("evaluation_dataset_releases") as batch_op:
        batch_op.drop_column("dataset_jsonl")
