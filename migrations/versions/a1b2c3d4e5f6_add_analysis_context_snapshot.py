"""add versioned sector context to analyses

Revision ID: a1b2c3d4e5f6
Revises: f6c7d8e9a0b1
Create Date: 2026-08-03 18:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f6c7d8e9a0b1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("analyses", schema=None) as batch_op:
        batch_op.drop_index("ix_analyses_cache_key")
        batch_op.add_column(sa.Column("context_snapshot", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("context_version", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("context_sha256", sa.String(length=64), nullable=True))
        batch_op.create_index(
            "ix_analyses_context_sha256", ["context_sha256"], unique=False
        )
        batch_op.create_index(
            "ix_analyses_cache_key",
            [
                "transcript_hash",
                "prompt_version",
                "score_version",
                "model",
                "context_sha256",
            ],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("analyses", schema=None) as batch_op:
        batch_op.drop_index("ix_analyses_cache_key")
        batch_op.drop_index("ix_analyses_context_sha256")
        batch_op.drop_column("context_sha256")
        batch_op.drop_column("context_version")
        batch_op.drop_column("context_snapshot")
        batch_op.create_index(
            "ix_analyses_cache_key",
            ["transcript_hash", "prompt_version", "score_version", "model"],
            unique=False,
        )
