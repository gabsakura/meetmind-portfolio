"""Add canonical ingestion trace to meeting submissions.

Revision ID: f1a2b3c4d5e6
Revises: c4e8f1a2b3d5
"""

import sqlalchemy as sa
from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "c4e8f1a2b3d5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "meeting_submissions",
        sa.Column(
            "protocol_version",
            sa.String(length=50),
            nullable=False,
            server_default="legacy-review-v1",
        ),
    )
    op.add_column(
        "meeting_submissions",
        sa.Column("canonical_sha256", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_meeting_submissions_canonical_sha256",
        "meeting_submissions",
        ["canonical_sha256"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_meeting_submissions_canonical_sha256",
        table_name="meeting_submissions",
    )
    op.drop_column("meeting_submissions", "canonical_sha256")
    op.drop_column("meeting_submissions", "protocol_version")
