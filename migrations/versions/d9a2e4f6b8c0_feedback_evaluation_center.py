"""feedback evaluation center and dataset releases

Revision ID: d9a2e4f6b8c0
Revises: c8f1d2e3a4b5
Create Date: 2026-08-07 21:30:00
"""

import sqlalchemy as sa
from alembic import op

revision = "d9a2e4f6b8c0"
down_revision = "c8f1d2e3a4b5"
branch_labels = None
depends_on = None

NEW_RLS_TABLES = (
    "feedback_reviews",
    "evaluation_cases",
    "evaluation_dataset_releases",
)


def _tenant_columns():
    return [
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
    ]


def _index(table: str, column: str, *, unique: bool = False) -> None:
    op.create_index(f"ix_{table}_{column}", table, [column], unique=unique)


def upgrade():
    with op.batch_alter_table("analysis_feedback") as batch_op:
        batch_op.add_column(
            sa.Column("status", sa.String(30), nullable=False, server_default="pending_review")
        )
        batch_op.add_column(
            sa.Column("review_generation", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(sa.Column("snapshot_model", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("snapshot_prompt_version", sa.String(50), nullable=True))
        batch_op.add_column(sa.Column("snapshot_playbook", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("snapshot_context_sha256", sa.String(64), nullable=True))
        batch_op.create_index("ix_analysis_feedback_status", ["status"])
        batch_op.create_index("ix_analysis_feedback_snapshot_playbook", ["snapshot_playbook"])

    op.create_table(
        "feedback_reviews",
        *_tenant_columns(),
        sa.Column("feedback_id", sa.String(36), nullable=False),
        sa.Column("reviewer_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("expected_result", sa.JSON(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["feedback_id"], ["analysis_feedback.id"]),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "feedback_id", "reviewer_id", "generation", name="uq_feedback_review_generation"
        ),
    )
    for column in ("organization_id", "feedback_id", "reviewer_id", "decision"):
        _index("feedback_reviews", column)

    op.create_table(
        "evaluation_cases",
        *_tenant_columns(),
        sa.Column("analysis_id", sa.String(36), nullable=False),
        sa.Column("feedback_id", sa.String(36), nullable=False),
        sa.Column("expected_result", sa.JSON(), nullable=False),
        sa.Column("sector", sa.String(100), nullable=True),
        sa.Column("region", sa.String(100), nullable=True),
        sa.Column("state", sa.String(10), nullable=True),
        sa.Column("approved_by_id", sa.String(36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"]),
        sa.ForeignKeyConstraint(["feedback_id"], ["analysis_feedback.id"]),
        sa.ForeignKeyConstraint(["approved_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_id", name="uq_evaluation_case_analysis"),
    )
    for column in (
        "organization_id", "analysis_id", "feedback_id", "sector", "region", "state"
    ):
        _index("evaluation_cases", column)

    op.create_table(
        "evaluation_dataset_releases",
        *_tenant_columns(),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("case_ids", sa.JSON(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "version", name="uq_dataset_release_org_version"
        ),
    )
    for column in (
        "organization_id", "version", "status", "manifest_sha256", "created_by_id"
    ):
        _index("evaluation_dataset_releases", column)

    if op.get_bind().dialect.name == "postgresql":
        for table in NEW_RLS_TABLES:
            op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
            op.execute(
                sa.text(
                    f"CREATE POLICY meetmind_tenant_isolation ON {table} "
                    "USING (organization_id = NULLIF("
                    "current_setting('meetmind.organization_id', true), '')::varchar) "
                    "WITH CHECK (organization_id = NULLIF("
                    "current_setting('meetmind.organization_id', true), '')::varchar)"
                )
            )


def downgrade():
    for table in reversed(NEW_RLS_TABLES):
        op.drop_table(table)
    with op.batch_alter_table("analysis_feedback") as batch_op:
        batch_op.drop_index("ix_analysis_feedback_snapshot_playbook")
        batch_op.drop_index("ix_analysis_feedback_status")
        batch_op.drop_column("snapshot_context_sha256")
        batch_op.drop_column("snapshot_playbook")
        batch_op.drop_column("snapshot_prompt_version")
        batch_op.drop_column("snapshot_model")
        batch_op.drop_column("review_generation")
        batch_op.drop_column("status")
