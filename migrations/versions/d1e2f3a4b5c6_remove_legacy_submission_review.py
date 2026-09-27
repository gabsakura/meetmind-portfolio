"""Remove the retired human submission review schema.

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
"""

import sqlalchemy as sa
from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


LEGACY_STATUSES = (
    "pending_review",
    "rejected",
    "approved",
    "verifying",
    "needs_review",
)


def upgrade():
    # Historical records that already produced a canonical meeting remain completed.
    # Incomplete review records become terminal instead of silently re-entering the
    # automatic pipeline without their original validation context.
    op.execute(
        sa.text(
            """
            UPDATE meeting_submissions
               SET status = CASE
                   WHEN meeting_id IS NOT NULL THEN 'completed'
                   WHEN status = 'rejected' THEN 'blocked'
                   ELSE 'failed'
               END,
                   error_code = CASE
                   WHEN meeting_id IS NOT NULL THEN error_code
                   ELSE COALESCE(error_code, 'legacy_review_retired')
               END
             WHERE status IN :legacy_statuses
            """
        ).bindparams(sa.bindparam("legacy_statuses", expanding=True, value=LEGACY_STATUSES))
    )

    with op.batch_alter_table("meeting_submissions", schema=None) as batch_op:
        batch_op.drop_index("ix_meeting_submissions_reviewed_by_id")
        batch_op.drop_index("ix_meeting_submissions_file_verification_id")
        batch_op.drop_column("reviewed_by_id")
        batch_op.drop_column("file_verification_id")
        batch_op.drop_column("repair_plan")
        batch_op.drop_column("review_note")
        batch_op.drop_column("reviewed_at")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index("ix_users_can_review_submissions")
        batch_op.drop_column("can_review_submissions")

    op.drop_table("file_verifications")
    if op.get_bind().dialect.name == "postgresql":
        sa.Enum(name="verificationstatus").drop(op.get_bind(), checkfirst=True)


def downgrade():
    verification_status = sa.Enum(
        "QUEUED",
        "RUNNING",
        "BLOCKED",
        "REJECTED",
        "NEEDS_REVIEW",
        "APPROVED",
        "FAILED",
        name="verificationstatus",
    )
    op.create_table(
        "file_verifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=50), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("status", verification_status, nullable=False),
        sa.Column("guard_report", sa.JSON(), nullable=True),
        sa.Column("antivirus_report", sa.JSON(), nullable=True),
        sa.Column("ai_result", sa.JSON(), nullable=True),
        sa.Column("clean_text", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("reuse_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("meeting_id", sa.String(length=36), nullable=True),
        sa.Column("created_by_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("organization_id", "content_hash", "created_by_id", "meeting_id", "status"):
        op.create_index(
            f"ix_file_verifications_{column}",
            "file_verifications",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_file_verifications_cache_key",
        "file_verifications",
        ["organization_id", "content_hash", "prompt_version", "model"],
        unique=True,
    )

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
            "ix_users_can_review_submissions",
            ["can_review_submissions"],
            unique=False,
        )

    with op.batch_alter_table("meeting_submissions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reviewed_by_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("file_verification_id", sa.String(length=36), nullable=True))
        batch_op.add_column(
            sa.Column("repair_plan", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.add_column(sa.Column("review_note", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            "fk_meeting_submissions_reviewed_by_id_users",
            "users",
            ["reviewed_by_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_meeting_submissions_file_verification_id_file_verifications",
            "file_verifications",
            ["file_verification_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_meeting_submissions_reviewed_by_id", ["reviewed_by_id"], unique=False
        )
        batch_op.create_index(
            "ix_meeting_submissions_file_verification_id",
            ["file_verification_id"],
            unique=False,
        )
