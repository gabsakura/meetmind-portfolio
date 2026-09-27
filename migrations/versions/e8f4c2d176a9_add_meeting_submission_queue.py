"""add meeting submission queue

Revision ID: e8f4c2d176a9
Revises: d4a1b8f37e60
Create Date: 2026-07-28 18:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "e8f4c2d176a9"
down_revision = "d4a1b8f37e60"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "can_review_submissions",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index(
            batch_op.f("ix_users_can_review_submissions"),
            ["can_review_submissions"],
            unique=False,
        )
    op.execute(
        sa.text(
            "UPDATE users SET can_review_submissions = :enabled "
            "WHERE CAST(role AS VARCHAR) = :admin_role"
        ).bindparams(enabled=True, admin_role="ADMIN")
    )

    op.create_table(
        "meeting_submissions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by_id", sa.String(length=36), nullable=False),
        sa.Column("reviewed_by_id", sa.String(length=36), nullable=True),
        sa.Column("file_verification_id", sa.String(length=36), nullable=True),
        sa.Column("meeting_id", sa.String(length=36), nullable=True),
        sa.Column("analysis_id", sa.String(length=36), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("file_format", sa.String(length=20), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("stored_object_key", sa.String(length=100), nullable=True),
        sa.Column("retained_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proposed_metadata", sa.JSON(), nullable=False),
        sa.Column("parser_report", sa.JSON(), nullable=False),
        sa.Column("mapping_json", sa.JSON(), nullable=False),
        sa.Column("sanitized_preview", sa.Text(), nullable=True),
        sa.Column("guard_report", sa.JSON(), nullable=False),
        sa.Column("antivirus_report", sa.JSON(), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("notification_status", sa.String(length=30), nullable=False),
        sa.Column("notification_attempts", sa.Integer(), nullable=False),
        sa.Column("notification_error", sa.String(length=200), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["file_verification_id"], ["file_verifications.id"]),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stored_object_key"),
    )
    for column in (
        "analysis_id",
        "created_by_id",
        "file_format",
        "file_sha256",
        "file_verification_id",
        "meeting_id",
        "notification_status",
        "retained_until",
        "reviewed_by_id",
        "status",
    ):
        op.create_index(
            f"ix_meeting_submissions_{column}",
            "meeting_submissions",
            [column],
            unique=False,
        )


def downgrade():
    op.drop_table("meeting_submissions")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_can_review_submissions"))
        batch_op.drop_column("can_review_submissions")
