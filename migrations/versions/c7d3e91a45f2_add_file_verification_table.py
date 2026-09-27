"""add file verification table

Revision ID: c7d3e91a45f2
Revises: 2f49a6d091b8
Create Date: 2026-07-27 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "c7d3e91a45f2"
down_revision = "2f49a6d091b8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "file_verifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=50), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "BLOCKED",
                "REJECTED",
                "NEEDS_REVIEW",
                "APPROVED",
                "FAILED",
                name="verificationstatus",
            ),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_file_verifications_created_by_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["meeting_id"],
            ["meetings.id"],
            name=op.f("fk_file_verifications_meeting_id_meetings"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_verifications")),
    )
    with op.batch_alter_table("file_verifications", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_file_verifications_content_hash"),
            ["content_hash"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_file_verifications_created_by_id"),
            ["created_by_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_file_verifications_meeting_id"), ["meeting_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_file_verifications_status"), ["status"], unique=False
        )
        # chave de deduplicação: o mesmo conteúdo, sob a mesma política e modelo,
        # só pode ter um veredito — é o que garante que o botão de diagnóstico
        # não consuma tokens duas vezes.
        batch_op.create_index(
            "ix_file_verifications_cache_key",
            ["content_hash", "prompt_version", "model"],
            unique=True,
        )


def downgrade():
    with op.batch_alter_table("file_verifications", schema=None) as batch_op:
        batch_op.drop_index("ix_file_verifications_cache_key")
        batch_op.drop_index(batch_op.f("ix_file_verifications_status"))
        batch_op.drop_index(batch_op.f("ix_file_verifications_meeting_id"))
        batch_op.drop_index(batch_op.f("ix_file_verifications_created_by_id"))
        batch_op.drop_index(batch_op.f("ix_file_verifications_content_hash"))
    op.drop_table("file_verifications")
    sa.Enum(name="verificationstatus").drop(op.get_bind(), checkfirst=True)
