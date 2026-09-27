"""Add versioned Gemini analysis modes.

Revision ID: a61f7c8d9e20
Revises: e0b3f5a7c9d1
"""

import sqlalchemy as sa
from alembic import op

revision = "a61f7c8d9e20"
down_revision = "e0b3f5a7c9d1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("analyses") as batch_op:
        batch_op.add_column(
            sa.Column(
                "mode_strategy",
                sa.String(length=20),
                nullable=False,
                server_default="auto",
            )
        )
        batch_op.add_column(
            sa.Column(
                "requested_modes",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(sa.Column("selected_modes", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "mode_bundle_version",
                sa.String(length=50),
                nullable=False,
                server_default="meetmind-transparent-v4",
            )
        )
        batch_op.add_column(
            sa.Column(
                "analysis_profile_hash",
                sa.String(length=64),
                nullable=False,
                server_default="legacy",
            )
        )
        batch_op.drop_index("ix_analyses_cache_key")
        batch_op.create_index(
            "ix_analyses_cache_key",
            [
                "transcript_hash",
                "prompt_version",
                "score_version",
                "model",
                "context_sha256",
                "analysis_profile_hash",
            ],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("analyses") as batch_op:
        batch_op.drop_index("ix_analyses_cache_key")
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
        batch_op.drop_column("analysis_profile_hash")
        batch_op.drop_column("mode_bundle_version")
        batch_op.drop_column("selected_modes")
        batch_op.drop_column("requested_modes")
        batch_op.drop_column("mode_strategy")
